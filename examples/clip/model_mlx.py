import mithril as ml
from typing import Optional, Any
from mithril import IOKey
from mithril.models import (
    Arange,
    ArgMax,
    Buffer,
    Cast,
    Concat,
    Convolution2D,
    Embedding,
    Flatten,
    GroupNorm,
    LayerNorm,
    Linear,
    Max,
    MaxPool2D,
    Mean,
    Model,
    Power,
    Randn,
    Relu,
    Reshape,
    ScaledDotProduct,
    Sigmoid,
    Sqrt,
    Squeeze,
    Sum,
    Tensor,
    Transpose,
    Where,
    ZerosLike,
    Softmax,
)

    
backend_torch = ml.TorchBackend()

def quick_gelu(name: str | None = None):
    block = Model(name=name)
    input = IOKey("input")
    block |= Sigmoid()((1.702 * input), output="sigmoid")
    block |= Buffer()(input * block.sigmoid, output=IOKey("output"))  # type: ignore
    return block
    
def attention(
    dims: int,
    num_heads: int,
    query_input_dims: Optional[int] = None,
    key_input_dims: Optional[int] = None,
    value_input_dims: Optional[int] = None,
    value_dims: Optional[int] = None,
    value_output_dims: Optional[int] = None,
    bias: bool = False,
    use_mask: bool = False,
    name: str | None = None

):
    block = Model(name=name)    
    query_input_dims = query_input_dims or dims
    key_input_dims = key_input_dims or dims
    value_input_dims = value_input_dims or key_input_dims
    value_dims = value_dims or dims
    value_output_dims = value_output_dims or dims
    
    queries = IOKey("queries")
    keys = IOKey("keys")
    values = IOKey("values")

    block |= Linear(dims, name="q_proj", use_bias=bias)(
        queries, output="queries_proj"
    )
    block |= Linear(dims, name="k_proj", use_bias=bias)(
        keys, output="keys_proj"
    )
    block |= Linear(value_dims, name="v_proj", use_bias=bias)(
        values, output="values_proj"
    )

    queries: ml.Connection = block.queries_proj  # type: ignore
    keys: ml.Connection = block.keys_proj  # type: ignore
    values: ml.Connection = block.values_proj  # type: ignore

    B, L = queries.shape[0], queries.shape[1]
    S = keys.shape[1]
    queries = queries.reshape((B, L, num_heads, -1)).transpose((0, 2, 1, 3))  # type: ignore
    keys = keys.reshape((B, S, num_heads, -1)).transpose((0, 2, 1, 3))  # type: ignore
    values = values.reshape((B, S, num_heads, -1)).transpose((0, 2, 1, 3))  # type: ignore
   
    if use_mask:
        block |= (mask_model := build_attention_mask())
        block |= ScaledDotProduct(is_causal=False, use_attn_mask=True)(
            query=queries,
            key=keys,
            value=values,
            attn_mask=mask_model.cout,
            output="scores",
        )
    else:
        block |= ScaledDotProduct(is_causal=False, use_attn_mask=False)(
            query=queries, key=keys, value=values, output="scores"
        )

    values_hat = block.scores.transpose((0, 2, 1, 3)).reshape((B, L, -1))
    block |= Linear(value_output_dims, name="out_proj")(values_hat, output="out")  # type: ignore
    block |= Buffer()(input=block.out, output=IOKey("output"))  # type: ignore
    return block
    
def mlp(config: dict[str, Any],
        name: str | None = None
):
    block = Model(name=name)
    block |= Linear(config["intermediate_size"], name="fc1")(input="input", output="fc1_output")
    block |= quick_gelu(name="activation_fn")(input=block.fc1_output, output="gelu_output")  # type: ignore
    block |= Linear(config["hidden_size"], name="fc2")(
        input=block.gelu_output,  # type: ignore
        output=IOKey("output"),
    )
    return block




def encode_layer(config: dict[str, Any],
                 use_mask: bool = False,
                 name: str | None = None):
    block = Model(name=name)
    input = IOKey("input")
    block |= LayerNorm(eps = config["layer_norm_eps"], name = "layer_norm1")(input = input, output = "ln_1_output")
    block |= attention(
        config["hidden_size"],config["num_attention_heads"], bias=True, use_mask=use_mask, name = "self_attn"
    )(
        queries = block.ln_1_output,
        keys = block.ln_1_output,
        values = block.ln_1_output,
        output = "attn_output"
    )
    block |= LayerNorm(eps = config["layer_norm_eps"], name = "layer_norm2")(input=input + block.attn_output, output = "ln_2_output")
    block |= mlp(config, name="mlp")(input=block.ln_2_output, output = "mlp_output")
    block |= Buffer()(
        input + block.attn_output + block.mlp_output,
        output = IOKey("output")
    )
    return block
    
def encoder(
    config: dict[str, Any],
    use_mask: bool = False,
    name: str | None = None,
):
    block = Model(name=name)
    input_key = "input"
    for idx in range(config["num_hidden_layers"]):
        block |= encode_layer(
            config, use_mask=use_mask, name=f"layers_{idx}"
        )(input=input_key, output=f"attn_output_{idx}")
        input_key = f"attn_output_{idx}"
    block |= Buffer()(input=f"attn_output_{idx}", output=IOKey("output"))
    return block

def text_embeddings(
        config: dict[str, Any],
        name: str | None = None,
):
    block = Model(name=name)
    input = IOKey("input")
    embed_dim = config["hidden_size"]
    block |= Embedding(config["vocab_size"], embed_dim, name="token_embedding")(
        input=input, output="token_embedding_output"
    )
    block |= Embedding(config["max_position_embeddings"], embed_dim, name="position_embedding")(
        input = input, weight = "position_embedding_weight", output="position_embedding_output"
    )
    block |= Buffer()(
        input = block.token_embedding_output+block.position_embedding_weight[: input.shape[1]],
        output = IOKey("output")
    )
    return block

def clip_text_model(
    config: dict[str, Any],
    name: str | None = None
):
    block= Model(name=name)
    input = IOKey("input")
    B, N = input.shape[0], input.shape[1]
    
    block |= text_embeddings(config,name = "embeddings")(
        input = input, output = "embeddings_output"
        )
    
    block |= encoder(config, use_mask=True, name="encoder")(
        input = block.embeddings_output, output = "t_encoder_output"
    )
    
    block|= LayerNorm(name="final_layer_norm")(input=block.t_encoder_output, output = "last_hidden_state")
    block |= Buffer()(input = block.last_hidden_state, output = IOKey("embed_out"))
    block |= Arange()(stop=B, output="arange_output")  # type: ignore
    block |= ArgMax(axis=-1)(input=input, output="argmax_output")
    
    # TODO: Add block.argmax_output
    block |= Buffer()(
        input = block.last_hidden_state[block.arange_output, block.argmax_output],  # type: ignore
        output = IOKey("output")
    )
    return block

def vision_embeddings(
    config: dict[str, Any],
    name: str | None = None
):
    block = Model(name=name)
    input = IOKey("input")
    batch_size = input.shape[0]
    c_embed_dim = config["hidden_size"]
    c_image_size = config["image_size"]
    c_patch_size = config["patch_size"]
    num_positions = ((c_image_size // c_patch_size) ** 2) + 1
    block |= (conv:=Convolution2D(kernel_size=c_patch_size, out_channels=c_embed_dim, stride=c_patch_size, use_bias=False, name ="patch_embedding"))(
        input = input, output = "patch_embeddings"
    )
    patch_embeddings:ml.Connection = block.patch_embeddings.reshape((batch_size,c_embed_dim,-1)).transpose((0, 2, 1))
    
    block |= Randn()(shape=(batch_size, 1, c_embed_dim), output="rand_1")  # type: ignore
    block |= ZerosLike()(input=block.rand_1, output="zeros_out")  # type: ignore
    class_embedding = IOKey("class_embedding", differentiable=True, shape=[c_embed_dim])
   
    block |= (concat:=Concat(axis=1))(input=[class_embedding + block.zeros_out, patch_embeddings], # type: ignore
                                      output = "embeddings" # type: ignore
                                      )
    block |= (embed:=Embedding(num_positions, c_embed_dim, name="position_embedding"))(
        input=input,
        weight="position_embedding_weight",
        output="position_embedding_output"
    )
    block |= (buff_o:=Buffer())(input = (block.embeddings + block.position_embedding_weight), output = IOKey("output"))
    return block

def clip_vision_model(
    config: dict[str, Any],
    name: str | None = None
):
    block = Model(name=name)
    input = IOKey("input")
    block |= vision_embeddings(config, name = "embeddings")(
        input = input, output = "v_embeddings_output"
    )
    block |= (ln_p:=LayerNorm(name="pre_layrnorm"))(input = block.v_embeddings_output, output = "pre_layrnorm_output")
    block |= (enc:=encoder(config,False, name="encoder"))(
        input = block.pre_layrnorm_output,
        output = "v_encoder_output"
    )
    block |= LayerNorm(name="post_layernorm")(input=block.v_encoder_output, output = "post_layernorm_output")
    block |= Buffer()(input =block.post_layernorm_output[:, 0, :], output=IOKey("output")) # type: ignore
    
    return block

# for torch.tensor.norm
def norm(
    p: str | int = 2,
    axis: int | None = None,
    keepdim: bool = False,
    name: str | None = None,
):
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
        assert isinstance(p, int)
        block += Sum(axis=axis, keepdim=keepdim)(input=(input.abs() ** p))
        block += Power(exponent=(1 / p))
    block += Buffer()(output=IOKey("output"))
    return block

def clip_model(
    config: dict[str, Any],
    name: str | None = None
):
    block = Model(name=name)
    input_ids = IOKey("input_ids")
    pixel_values = IOKey("pixel_values")
    text_embed_dim = config["text_config"]["hidden_size"]
    vision_embed_dim = config["vision_config"]["hidden_size"]
    projection_dim = config["projection_dim"]

    text_model = clip_text_model(config["text_config"], name="text_model")
    block |= text_model(
        input = input_ids, output="text_pooler_output"
    )
    text_projection_weight = IOKey("text_projection_weight", differentiable =True, shape =[text_embed_dim, projection_dim])
    
    text_projection_output = block.text_pooler_output @ text_projection_weight.transpose()
    block |= Buffer()(input=block.text_pooler_output, output=IOKey("t_p_output"))  # type: ignore

    block |= norm(p=2, axis=1, keepdim=True)(
        input = text_projection_output, output = "norm_text_output"
    )
    text_embeds =  text_projection_output / block.norm_text_output

    vision_model = clip_vision_model(config["vision_config"], name="vision_model")
    block |= vision_model(
        input = pixel_values, output="visual_pooler_output" 
    )
    
    # visual_projection_weight = IOKey("visual_projection_weight", differentiable =True, shape =[vision_embed_dim, projection_dim])
    # visual_projection_output = block.visual_pooler_output @ visual_projection_weight.transpose()
    block|= Linear(projection_dim,use_bias=False, name="visual_projection")(input = block.visual_pooler_output, output = "visual_projection_output")
    
    block |= norm(p=2, axis=1, keepdim=True)(
        input = block.visual_projection_output, output = "norm_visual_output"
    )
    image_embeds =  block.visual_projection_output / block.norm_visual_output
    
   
    block |= Buffer()(input=image_embeds, output=IOKey("image_embeds"))  # type: ignore
    block |= Buffer()(input=text_embeds, output=IOKey("text_embeds"))  # type: ignore
    return block
   






def build_attention_mask() -> Model:
    block = Model()
    block |= Arange(stop=77)(output="arange_out_1")
    block |= Arange(stop=77)(output="arange_out_2")
    upper_bool_triu = block.arange_out_1[..., None] >= block.arange_out_2[None, ...]  # type: ignore
    block |= Where()(
        cond=upper_bool_triu,
        input1=Tensor(0.0),
        input2=Tensor(float("-inf")),
        output=IOKey("output"),
    )
    return block


def main():
    backend = ml.TorchBackend("cuda")
    config = {
    "text_config": {
        # Transformer for text is identical across CLIP variants:
        "num_hidden_layers": 12,         # 12 layers in the text transformer.
        "hidden_size": 768,              # Text encoder hidden size remains 512.
        "intermediate_size": 3072,       # MLP expansion factor (4×512).
        "num_attention_heads": 12,        # 8 attention heads (512 / 8 = 64 per head).
        "max_position_embeddings": 77,   # Maximum token length (including special tokens).
        "vocab_size": 49408,             # Vocabulary size used by the tokenizer.
        "layer_norm_eps": 1e-5,          # Epsilon for numerical stability in LayerNorm.
    },
    "vision_config": {
        # Vision encoder is scaled up for the large model:
        "num_hidden_layers": 24,         # 24 transformer layers in the vision encoder.
        "hidden_size": 1024,             # Vision transformer hidden size.
        "intermediate_size": 4096,       # MLP expansion factor (4×1024).
        "num_attention_heads": 16,       # 16 attention heads (1024 / 16 = 64 per head).
        "num_channels": 3,               # RGB input images.
        "image_size": 224,               # Input resolution.
        "patch_size": 14,                # Patch size for ViT‑L/14.
        "layer_norm_eps": 1e-5,          # LayerNorm epsilon.
    },
    # Projection dimension for the joint embedding space.
    # For CLIP ViT‑L/14, the vision projection is typically 768.
    "projection_dim": 768,
    }
    m_model = clip_model(config)
    pm = ml.compile(
            m_model,
            backend=backend,
            shapes={"pixel_values":(1, 3 ,224, 224), "input_ids":(1, 77)},
            data_keys={"pixel_values","input_ids"},
            use_short_namings=False,
        )
    params = pm.randomize_params()
    for i in params.keys():
        print(i)

if __name__ == "__main__":
    main()

