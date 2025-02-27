import torch
from einops import rearrange
from copy import deepcopy
import mithril as ml
from mithril import IOKey
from mithril.models import *
from torch import nn

backend_torch = ml.TorchBackend(device="cuda")
from collections import OrderedDict
from typing import Tuple, Union
import sys


def quick_gelu(name: str | None = None):
    block = Model(name=name)
    input = IOKey("input")
    block |= Sigmoid()((1.702 * input), output="sigmoid")
    block |= Buffer()(input * block.sigmoid, output=IOKey("output"))
    return block


def multi_head_attention(
    d_model: int, n_head: int, use_attn_mask: bool = False, name: str | None = None
):
    block = Model(name=name)
    assert d_model % n_head == 0, "d_model is not divisible by h"
    queries = IOKey("queries")
    B, L = queries.shape[0], queries.shape[1]
    block |= Linear(3 * d_model, name="in_proj")(queries, output="in_proj")

    in_proj: ml.Connection = block.in_proj
    in_proj = in_proj.reshape((B, L, 3, -1))
    block |= Buffer()(input=in_proj, output="in_proj_buffer")

    queries = (
        in_proj[:, :, 0]
        .reshape((B, L, -1))
        .reshape((B, L, n_head, -1))
        .transpose((0, 2, 1, 3))
    )
    # queries = in_proj[:,:,0].reshape((B, L,-1))

    keys = (
        in_proj[:, :, 1]
        .reshape((B, L, -1))
        .reshape((B, L, n_head, -1))
        .transpose((0, 2, 1, 3))
    )
    values = (
        in_proj[:, :, 2]
        .reshape((B, L, -1))
        .reshape((B, L, n_head, -1))
        .transpose((0, 2, 1, 3))
    )
    block |= Buffer()(input=queries, output="query_buffer")
    block |= Buffer()(input=keys, output="key_buffer")
    block |= Buffer()(input=values, output="quer_proj_buffer")

    if use_attn_mask:
        block |= ScaledDotProduct(is_causal=False, use_attn_mask=True)(
            queries,
            keys,
            values,
            attn_mask=IOKey("mask", shape=(77, 77)),
            output="attention",
        )
    else:
        block |= ScaledDotProduct(is_causal=False)(
            queries, keys, values, output="attention"
        )
    values_hat = block.attention.transpose((0, 2, 1, 3)).reshape((B, L, -1))
    block |= Linear(d_model, name="out_proj")(values_hat, output=IOKey("output"))
    return block


def mlp_resblock(d_model: int, name: str | None = None):
    block = Model(name=name)
    input = IOKey("input")
    block |= Linear(d_model * 4, name="c_fc")(input="input", output="c_fc_output")
    block |= quick_gelu(name="gelu")(input=block.c_fc_output, output="gelu_output")
    block |= Linear(d_model, name="c_proj")(
        input=block.gelu_output, output=IOKey("output")
    )
    return block


def residual_attention_block(
    d_model: int, n_head: int, use_attn_mask: bool = False, name: str | None = None
):
    block = Model(name=name)
    assert d_model % n_head == 0, "d_model is not divisible by h"
    input = IOKey("input")
    block += LayerNorm(name="ln_1")(input="input", output="ln_1")
    attn = multi_head_attention(d_model, n_head, use_attn_mask, name="attn")
    if use_attn_mask:
        mask = IOKey("mask")
        block |= attn(queries=block.ln_1, mask=mask, output="attention")
    else:
        block |= attn(queries=block.ln_1, output="attention")

    block |= LayerNorm(name="ln_2")(input + block.attention, output="ln_2")
    mlp = mlp_resblock(d_model, name="mlp")
    block |= mlp(input=block.ln_2, output="mlp_output")
    block |= Buffer()(input + block.mlp_output, output=IOKey("output"))
    return block


def seq_resblocks(
    width: int,
    layers: int,
    heads: int,
    use_attn_mask: bool = False,
    name: str | None = None,
):
    block = Model(name=name)
    input = IOKey("input")
    input_key = "input"
    if use_attn_mask:
        mask = IOKey("mask")
        for idx in range(layers):
            block |= residual_attention_block(
                width, heads, use_attn_mask, name=f"{idx}"
            )(input=input_key, mask=mask, output=f"attn_output_{idx}")
            input_key = f"attn_output_{idx}"
    else:
        for idx in range(layers):
            block |= residual_attention_block(width, heads, name=f"{idx}")(
                input=input_key, output=f"attn_output_{idx}"
            )
            input_key = f"attn_output_{idx}"
    block |= Buffer()(input=f"attn_output_{idx}", output=IOKey("output"))
    return block


def transformer(
    width: int,
    layers: int,
    heads: int,
    use_attn_mask: bool = False,
    name: str | None = None,
):
    block = Model(name=name)
    input = IOKey("input")

    resblocks = seq_resblocks(
        width=width,
        layers=layers,
        heads=heads,
        use_attn_mask=use_attn_mask,
        name="resblocks",
    )
    if use_attn_mask:
        mask = IOKey("mask")
        block |= resblocks(input=input, mask=mask, output="resblocks_output")
    else:
        block |= resblocks(input=input, output="resblocks_output")

    block |= Buffer()(input=block.resblocks_output, output=IOKey("output"))

    return block


def vision_transformer(
    input_resolution: int,
    patch_size: int,
    width: int,
    layers: int,
    heads: int,
    output_dim: int,
    use_proj: bool = False,
    name: str | None = None,
):
    block = Model(name=name)
    input = IOKey("input", shape=(1, 3, 224, 224))

    block |= Convolution2D(
        kernel_size=patch_size,
        out_channels=width,
        stride=patch_size,
        use_bias=False,
        name="conv1",
    )(input=input, output="conv1")
    # shape_conv1 = block.conv1.shape

    conv1_r = block.conv1.reshape((1, 1024, 256)).transpose((0, 2, 1))

    # # self.class_embedding = nn.Parameter(scale * torch.randn(width))
    class_embedding = IOKey("class_embedding", differentiable=True, shape=(1, 1, 1024))
    block |= Concat(axis=1)(input=[class_embedding, conv1_r], output="cat1")

    positional_embedding = IOKey(
        "positional_embedding", differentiable=True, shape=(257, 1024)
    )
    # block|= Concat(n=2,axis=1)(input1=positional_embedding,input2 = positional_embedding, output = "cat2")

    # # self.positional_embedding = nn.Parameter(scale * torch.randn((input_resolution // patch_size) ** 2 + 1, width))
    block |= LayerNorm(name="ln_pre")(
        input=block.cat1 + positional_embedding, output="ln_1"
    )

    transformer_visual = transformer(width, layers, heads, name="transformer")

    block |= transformer_visual(
        input=block.ln_1.transpose((1, 0, 2)), output="transformer"
    )

    block |= Transpose(axes=(1, 0, 2))(input=block.transformer, output="transformer_p")

    block |= LayerNorm(name="ln_post")(input=block.transformer_p, output="ln_post")

    if use_proj:
        # self.proj = nn.Parameter(scale * torch.randn(width, output_dim))
        block |= Buffer()(
            block.ln_post[:, 0, :]
            @ IOKey("proj", differentiable=True, shape=(1024, 768)),
            output=IOKey("output"),
        )
        return block

    block |= Buffer()(input=block.ln_post[:, 0, :], output=IOKey("output"))
    return block


def multi_head_attention_forward(
    embed_dim_to_check: int, num_heads: int, dropout_p: float
):
    block = Model()
    query = IOKey("query", shape=(1, 1, 2048))
    key = IOKey("key", shape=(50, 1, 2048))
    value = IOKey("value", shape=(50, 1, 2048))
    q_proj_weight = IOKey("q_proj_weight", shape=(2048, 2048))
    k_proj_weight = IOKey("k_proj_weight", shape=(2048, 2048))
    v_proj_weight = IOKey("v_proj_weight", shape=(2048, 2048))
    in_proj_bias = IOKey("in_proj_bias", type=ml.Tensor, shape=(6144,))
    out_proj_weight = IOKey("out_proj_weight", shape=(1024, 2048))
    out_proj_bias = IOKey("out_proj_bias", type=ml.Tensor, shape=(1024,))

    tgt_len, bsz, embed_dim = query.shape[0], query.shape[1], query.shape[2]

    # assert embed_dim == embed_dim_to_check, "Embedding dimension mismatch."

    head_dim = embed_dim // num_heads
    # assert (head_dim * num_heads) == embed_dim, "embed_dim must be divisible by num_heads"
    scaling = head_dim**-0.5

    q = (query @ q_proj_weight.transpose() + in_proj_bias[0:embed_dim]) * scaling
    block |= Buffer()(input=q)
    k = (
        key @ k_proj_weight.transpose()
        + in_proj_bias[embed_dim_to_check : 2 * embed_dim_to_check]
    )
    block |= Buffer()(input=k)

    v = (
        value @ v_proj_weight.transpose()
        + in_proj_bias[2 * embed_dim_to_check : 3 * embed_dim_to_check]
    )
    block |= Buffer()(input=v)

    q_r = q.reshape((tgt_len, bsz * num_heads, head_dim)).transpose((1, 0, 2))
    block |= Buffer()(input=q_r)

    k_r = k.reshape((-1, bsz * num_heads, head_dim)).transpose((1, 0, 2))
    block |= Buffer()(input=k_r)

    v_r = v.reshape((-1, bsz * num_heads, head_dim)).transpose((1, 0, 2))
    block |= Buffer()(input=v_r)

    block |= ScaledDotProduct(is_causal=False)(
        query=q_r, key=k_r, value=v_r, output="attention"
    )

    attn_output = block.attention.transpose((1, 0, 2)).reshape(
        (tgt_len, bsz, embed_dim)
    )
    attn_output = attn_output @ out_proj_weight.transpose() + out_proj_bias

    block |= Buffer()(input=attn_output, output=IOKey("output"))

    return block


def attention_pool2d(
    spacial_dim: int,
    embed_dim: int,
    num_heads: int,
    output_dim: int = None,
    name: str | None = None,
):
    block = Model(name=name)
    input = IOKey("input", shape=(1, 2048, 7, 7))
    """
    self.positional_embedding = nn.Parameter(torch.randn(spacial_dim ** 2 + 1, embed_dim) / embed_dim ** 0.5)
    self.k_proj = nn.Linear(embed_dim, embed_dim)
    self.q_proj = nn.Linear(embed_dim, embed_dim)
    self.v_proj = nn.Linear(embed_dim, embed_dim)
    self.c_proj = nn.Linear(embed_dim, output_dim or embed_dim)
    """
    # q_proj_weight=self.q_proj.weight
    q_proj_weight = IOKey("q_proj_weight", shape=(2048, 2048))
    # q_proj_weight=self.q_proj.weight
    k_proj_weight = IOKey("k_proj_weight", shape=(2048, 2048))
    # q_proj_weight=self.q_proj.weight
    v_proj_weight = IOKey("v_proj_weight", shape=(2048, 2048))
    # q_proj_weight=self.q_proj.weight
    in_proj_bias = IOKey("in_proj_bias", type=ml.Tensor, shape=(6144,))
    # q_proj_weight=self.q_proj.weight
    out_proj_weight = IOKey("out_proj_weight", shape=(1024, 2048))
    # q_proj_weight=self.q_proj.weight
    out_proj_bias = IOKey("out_proj_bias", type=ml.Tensor, shape=(1024,))
    # q_proj_weight=self.q_proj.weight
    positional_embedding = IOKey(
        "positional_embedding", type=ml.Tensor, shape=(50, 2048)
    )
    block |= Flatten(start_dim=2)(input=input)
    block += Transpose(axes=(2, 0, 1))(output="flatten")
    block |= Mean(axis=0, keepdim=True)(block.flatten, output="mean")
    block |= Concat(axis=0)([block.flatten, block.mean], output="cn1")
    block |= Buffer()(block.cn1 + positional_embedding[:, None, :], output="x")
    # I need to place block.x.shape[-1] instead of embed_dim but when I put it in the code it gives
    # Requires accessible connection to be processe error
    multi_head_attention_forward = multi_head_attention_forward(
        embed_dim, num_heads, 0.0
    )

    block |= multi_head_attention_forward(
        query=block.x[:1],
        key=block.x,
        value=block.x,
        q_proj_weight=q_proj_weight,
        k_proj_weight=k_proj_weight,
        v_proj_weight=v_proj_weight,
        in_proj_bias=in_proj_bias,
        out_proj_weight=out_proj_weight,
        out_proj_bias=out_proj_bias,
        output="attention",
    )
    block |= Squeeze()(input=block.attention, output=IOKey("output"))
    return block


def bottleneck(inplanes: int, planes: int, stride: int = 1, name: str | None = None):
    block = Model(name=name)
    expansion = 4
    input = IOKey("input")

    block += Convolution2D(
        kernel_size=1, out_channels=planes, use_bias=False, name="conv1"
    )(input="input")
    # nn.BatchNorm2d(planes)
    block += GroupNorm(num_groups=1, name="bn1")
    block += Relu(name="relu1")
    block += Convolution2D(
        kernel_size=3, out_channels=planes, padding=1, use_bias=False, name="conv2"
    )
    # nn.BatchNorm2d(planes)
    block += GroupNorm(num_groups=1, name="bn2")
    block += Relu(name="relu2")
    if stride > 1:
        # nn.AvgPool2d(stride)
        # nn.AvgPool2d(stride) if stride > 1 else nn.Identity()
        block += MaxPool2D(stride, name="avgpool")

    block += Convolution2D(
        kernel_size=1, out_channels=planes * expansion, use_bias=False, name="conv3"
    )
    # nn.BatchNorm2d(planes)
    block += GroupNorm(num_groups=1, name="bn3")(output="out1")

    if stride > 1 or inplanes != planes * expansion:
        # nn.AvgPool2d(stride)
        block |= MaxPool2D(stride, name=f"downsample_{-1}")(
            input, output="downsample_pool"
        )
        block |= Convolution2D(
            kernel_size=1,
            out_channels=planes * expansion,
            use_bias=False,
            name=f"downsample_{0}",
        )(block.downsample_pool, output="downsample_conv")
        # nn.BatchNorm2d(planes)
        block |= GroupNorm(num_groups=1, name=f"downsample_{1}")(
            block.downsample_conv, output="out2"
        )
        out = block.out1 + block.out2

    else:
        out = block.out1 + input
    block |= Relu(name="relu3")(out, output=IOKey("output"))
    block.set_cout("output")

    return block


def make_layer(inplanes, planes, blocks, stride=1, name: str | None = None):
    block = Model(name=name)
    input = IOKey("input")
    block |= bottleneck(inplanes, planes, stride)(input=input, output="bottle_neck0")
    _inplanes = 4 * inplanes
    input_key = "bottle_neck0"
    for i in range(1, blocks):
        block |= bottleneck(_inplanes, planes)(
            input=input_key, output=f"bottle_neck{i}"
        )
        input_key = f"bottle_neck{i}"
    block |= Buffer()(input=f"bottle_neck{i}", output=IOKey("output"))
    return block


def modified_resnet(
    layers, output_dim, heads, input_resolution=224, width=64, name: str | None = None
):
    block = Model(name=name)
    input = IOKey("input", shape=(1, 3, 224, 224))
    # for attn_pool
    # q_proj_weight=self.q_proj.weight
    q_proj_weight = IOKey("q_proj_weight", shape=(2048, 2048))
    # q_proj_weight=self.q_proj.weight
    k_proj_weight = IOKey("k_proj_weight", shape=(2048, 2048))
    # q_proj_weight=self.q_proj.weight
    v_proj_weight = IOKey("v_proj_weight", shape=(2048, 2048))
    # q_proj_weight=self.q_proj.weight
    in_proj_bias = IOKey("in_proj_bias", shape=(6144,))
    # q_proj_weight=self.q_proj.weight
    out_proj_weight = IOKey("out_proj_weight", shape=(1024, 2048))
    # q_proj_weight=self.q_proj.weight
    out_proj_bias = IOKey("out_proj_bias", shape=(1024,))
    # q_proj_weight=self.q_proj.weight
    positional_embedding = IOKey("positional_embedding", shape=(50, 2048))

    # x = x.type(self.conv1.weight.dtype)
    block |= Convolution2D(
        kernel_size=3,
        out_channels=width // 2,
        stride=2,
        padding=1,
        use_bias=False,
        name="conv1",
    )(input=input, output="conv_out_1")
    # nn.BatchNorm2d(width // 2)
    block |= GroupNorm(num_groups=1, name="bn1")(
        input="conv_out_1", output="norm_out_1"
    )
    block |= Relu(name="relu1")(input="norm_out_1", output="rl_out_1")
    block |= Convolution2D(
        kernel_size=3, out_channels=width // 2, padding=1, use_bias=False, name="conv2"
    )(input="rl_out_1", output="conv_out_2")

    # nn.BatchNorm2d(width // 2)
    block |= GroupNorm(num_groups=1, name="bn2")(
        input="conv_out_2", output="norm_out_2"
    )
    block |= Relu(name="relu2")(input="norm_out_2", output="rl_out_2")
    block |= Convolution2D(
        kernel_size=3, out_channels=width, padding=1, use_bias=False, name="conv3"
    )(input="rl_out_2", output="conv_out_3")

    # nn.BatchNorm2d(width)
    block |= GroupNorm(num_groups=1, name="bn3")(
        input="conv_out_3", output="norm_out_3"
    )
    block |= Relu(name="relu3")(input="norm_out_3", output="rl_out_3")
    # nn.AvgPool2d(2)

    block |= MaxPool2D(kernel_size=2, name="avgpool")(
        input="rl_out_3", output="avgpool_out"
    )
    make_layer_block = make_layer(width, width, layers[0], name="layer1")
    input_key = "make_layer_0"
    block |= make_layer_block(input=block.avgpool_out, output=input_key)

    for idx in range(1, 4):
        make_layer_block = make_layer(
            width, width * (2**idx), layers[idx], stride=2, name=f"layer{idx+1}"
        )
        block |= make_layer_block(input=input_key, output=f"make_layer_{idx}")
        input_key = f"make_layer_{idx}"
    attnpool = attention_pool2d(
        input_resolution // 32, width * 32, heads, output_dim, name="attnpool"
    )
    block |= attnpool(
        input=input_key,
        q_proj_weight=q_proj_weight,
        k_proj_weight=k_proj_weight,
        v_proj_weight=v_proj_weight,
        in_proj_bias=in_proj_bias,
        out_proj_weight=out_proj_weight,
        out_proj_bias=out_proj_bias,
        positional_embedding=positional_embedding,
        output="attn_output",
    )
    block |= Reshape(shape=(1, 1024))(input=block.attn_output, output=IOKey("output"))
    return block


# for torch.tensor.norm
def norm(p=2, axis: int = None, keepdim: bool = False, name: str | None = None):
    block = Model(name=name)
    input = IOKey("input")
    if p == "inf":
        block += Max(axis=axis, keepdim=keepdim)(input=input.abs())
    elif p == 1:
        block += Sum(axis=axis, keepdim=keepdim)(input=input.abs())
    elif p == 2:
        block += Sum(axis=axis, keepdim=keepdim)(input=(input**2))
        block += Sqrt()
    else:
        block += Sum(axis=axis, keepdim=keepdim)(input=(input.abs() ** p))
        block += Power(exponent=(1 / p))
    block += Buffer()(output=IOKey("output"))
    return block


def eot_creator(batch_number: int, name: str | None = None):
    block = Model(name=name)
    input = IOKey("input", type=ml.Tensor)
    text = IOKey("text", type=ml.Tensor)
    block |= ArgMax(axis=-1)(input="text", output="argmax")
    block |= Buffer()(input=input[None, 0, 0], output=f"batch_max_{0}")
    input_key = f"batch_max_{0}"
    for i in range(batch_number):
        block |= Concat(axis=0)(
            input=[input_key, input[None, i + 1, 0]], output=f"batch_max_{i+1}"
        )
        input_key = f"batch_max_{i+1}"
    block |= Buffer()(input_key, output=IOKey("output"))
    return block


def clip(
    embed_dim: int,
    # vision
    image_resolution: int,
    vision_layers: Union[Tuple[int, int, int, int], int],
    vision_width: int,
    vision_patch_size: int,
    # text
    context_length: int,
    vocab_size: int,
    transformer_width: int,
    transformer_heads: int,
    transformer_layers: int,
    name: str | None = None,
):
    block = Model(name=name)
    image = IOKey("image", type=ml.Tensor)
    text = IOKey("text", type=ml.Tensor)

    if isinstance(vision_layers, (tuple, list)):
        vision_heads = vision_width * 32 // 64
        visual = modified_resnet(
            layers=vision_layers,
            output_dim=embed_dim,
            heads=vision_heads,
            input_resolution=image_resolution,
            width=vision_width,
            name="visual",
        )
        q_proj_weight = IOKey("q_proj_weight", shape=(2048, 2048))
        # q_proj_weight=self.q_proj.weight
        k_proj_weight = IOKey("k_proj_weight", shape=(2048, 2048))
        # q_proj_weight=self.q_proj.weight
        v_proj_weight = IOKey("v_proj_weight", shape=(2048, 2048))
        # q_proj_weight=self.q_proj.weight
        in_proj_bias = IOKey("in_proj_bias", shape=(6144,))
        # q_proj_weight=self.q_proj.weight
        out_proj_weight = IOKey("out_proj_weight", shape=(1024, 2048))
        # q_proj_weight=self.q_proj.weight
        out_proj_bias = IOKey("out_proj_bias", shape=(1024,))
        # q_proj_weight=self.q_proj.weight
        positional_embedding_visual = IOKey(
            "positional_embeddin_visual", shape=(50, 2048)
        )
        block |= visual(
            input="image",
            q_proj_weight=q_proj_weight,
            k_proj_weight=k_proj_weight,
            v_proj_weight=v_proj_weight,
            in_proj_bias=in_proj_bias,
            out_proj_weight=out_proj_weight,
            out_proj_bias=out_proj_bias,
            positional_embedding=positional_embedding_visual,
            output="image_features",
        )

    else:
        vision_heads = vision_width // 64
        visual = vision_transformer(
            input_resolution=image_resolution,
            patch_size=vision_patch_size,
            width=vision_width,
            layers=vision_layers,
            heads=vision_heads,
            output_dim=embed_dim,
            use_proj=True,
            name="visual",
        )
        class_embedding = IOKey(
            "visual_class_embedding", type=ml.Tensor, differentiable=True
        )
        visual_positional_embedding = IOKey(
            "visual_positional_embedding", type=ml.Tensor, differentiable=True
        )
        proj = IOKey("visual_proj", type=ml.Tensor, differentiable=True)
        block |= visual(
            input="image",
            class_embedding="visual_class_embedding",
            positional_embedding="visual_positional_embedding",
            proj="visual_proj",
            output="image_features",
        )

    block |= Embedding(vocab_size, transformer_width, name="token_embedding")(
        input="text", output="token_embedding"
    )
    block |= Reshape(shape=(1, 77, 768))(
        input=block.token_embedding, output="token_embedding_reshaped"
    )
    # self.positional_embedding = nn.Parameter(torch.empty(self.context_length, transformer_width))
    positional_embedding = IOKey(
        "positional_embedding", type=ml.Tensor, differentiable=True
    )
    embedding = block.token_embedding_reshaped + positional_embedding
    mask = IOKey("mask", type=ml.Tensor)
    block |= Buffer()(input=embedding, output="embedding_output")
    transformer_main = transformer(
        width=transformer_width,
        layers=transformer_layers,
        heads=transformer_heads,
        use_attn_mask=True,
        name="transformer",
    )
    block |= transformer_main(input=embedding, mask=mask, output="transformer")

    block |= LayerNorm(name="ln_final")(
        block.transformer.transpose((1, 0, 2)), output="ln_final"
    )

    block |= eot_creator(0)(input=block.ln_final, text="text", output="eot_tokens")
    text_projection = IOKey("text_projection", type=ml.Tensor, differentiable=True)
    block |= Buffer()(text_projection, output="text_proj")
    # self.text_projection = nn.Parameter(torch.empty(transformer_width, embed_dim))
    block |= Buffer()(block.eot_tokens @ text_projection, output="prod")
    block |= Buffer()(input=block.eot_tokens @ text_projection, output="text_features")
    norm1 = norm(p=2, axis=1, keepdim=True)
    norm2 = norm(p=2, axis=1, keepdim=True)

    block |= Buffer()(block.image_features, output="img_feat")

    block |= norm1(input=block.image_features, output="image_features_norm")
    block |= norm2(input=block.text_features, output="text_features_norm")

    image_features = block.image_features / block.image_features_norm
    text_features = block.text_features / block.text_features_norm

    # self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
    logit_scale = IOKey("logit_scale", type=ml.Tensor, differentiable=True)
    logits_per_image = logit_scale.exp() * (image_features @ text_features.transpose())
    block |= Buffer()(
        input=(image_features @ text_features.transpose()), output="prdo_text_img"
    )
    logits_per_text = logits_per_image.transpose()
    block |= Buffer()(input=logits_per_image, output=IOKey("logits_per_image"))
    block |= Buffer()(input=logits_per_text, output=IOKey("logits_per_text"))

    return block


# clip_vit = clip(
#     embed_dim=768,  # Embedding dimension for ViT-L/14
#     image_resolution=224,  # Input image resolution
#     vision_layers=24,  # Number of transformer layers in the vision model
#     vision_width=1024,  # Width of the vision model
#     vision_patch_size=14,  # Patch size for the ViT-L/14 model
#     context_length=77,  # Maximum length of text input
#     vocab_size=49408,  # Size of the text tokenizer's vocabulary
#     transformer_width=768,  # Width of the text transformer
#     transformer_heads=12,  # Number of attention heads in the transformer
#     transformer_layers=12,  # Number of transformer layers for the text model
#     name="ViT-L/14_CLIP",  # Name of the model instance
# )


# clip_vit_model = ml.compile(
#     clip_vit,
#     backend_torch,
#     data_keys={"image", "text", "mask"},
#     shapes={
#         "image": (1, 3, 224, 224),
#         "text": (1, 77),
#         "visual_proj": (1024, 768),
#         "positional_embedding": (77, 768),
#         "visual_positional_embedding": (257, 1024),
#         "mask": (77, 77),
#         "text_projection": (768, 768),
#     },
#     inference=True,
#     use_short_namings=False,
# )


# params = clip_vit_model.randomize_params()

# for i in params.keys():
#     print(i)

# torch.manual_seed(42)
# image = torch.randn((1, 3, 224, 224)).cuda()
# text = torch.randn((1, 77)).cuda()
# proj = torch.randn((1024, 768)).cuda()
# class_embedding = torch.randn((1, 1, 1024)).cuda()
# positional_embedding_2 = torch.randn((257, 1024)).cuda()
# positional_embedding = torch.randn((1, 77, 768)).cuda()
# mask = torch.randn((77, 77)).cuda()
# text_projection = torch.randn((768, 768)).cuda()
# logit_scale = torch.randn((1,)).cuda()
# inputs = {
#     "image": image,
#     "text": text,
#     "proj": proj,
#     "class_embedding": class_embedding,
#     "positional_embedding": positional_embedding,
#     "positional_embedding_2": positional_embedding_2,
#     "mask": mask,
#     "text_projection": text_projection,
#     "logit_scale": logit_scale,
# }
# output = clip_vit_model.evaluate(params, inputs)

# print(output["logits_per_image"])
# print(output["logits_per_text"])
