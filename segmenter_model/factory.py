import os
from pathlib import Path

import yaml
from timm.models.helpers import load_pretrained, load_custom_pretrained
from timm.models.registry import register_model
from timm.models.vision_transformer import _create_vision_transformer
from timm.models.vision_transformer import default_cfgs, checkpoint_filter_fn

import segmenter_model.torch as ptu
import torch
from segmenter_model.decoder import MaskTransformer
from segmenter_model.segmenter import Segmenter
from segmenter_model.vit_dino import vit_small, VisionTransformer


@register_model
def vit_base_patch8_384(pretrained=False, **kwargs):
    """ViT-Base model (ViT-B/16) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 384x384, source https://github.com/google-research/vision_transformer.
    """
    model_kwargs = dict(patch_size=8, embed_dim=768, depth=12, num_heads=12, **kwargs)
    model = _create_vision_transformer(
        "vit_base_patch8_384",
        pretrained=pretrained,
        default_cfg=dict(
            url="",
            input_size=(3, 384, 384),
            mean=(0.5, 0.5, 0.5),
            std=(0.5, 0.5, 0.5),
            num_classes=1000,
        ),
        **model_kwargs,
    )
    return model


def create_vit(model_cfg):
    model_cfg = model_cfg.copy()
    backbone = model_cfg.pop("backbone")
    if 'pretrained_weights' in model_cfg:
        pretrained_weights = model_cfg.pop('pretrained_weights')

    if 'dino' in backbone:
        if backbone.lower() == 'dino_vits16':
            model_cfg['drop_rate'] = model_cfg['dropout']
            model = vit_small(**model_cfg)
            # hard-coded for now, too lazy
            pretrained_weights = 'dino_deitsmall16_pretrain.pth'
            if not os.path.exists(pretrained_weights):
                import urllib.request
                urllib.request.urlretrieve(
                    "https://dl.fbaipublicfiles.com/dino/dino_deitsmall16_pretrain/dino_deitsmall16_pretrain.pth",
                    pretrained_weights)
            model.load_state_dict(torch.load(pretrained_weights), strict=True)
        else:
            model = torch.hub.load('facebookresearch/dino:main', backbone)
        setattr(model, 'd_model', model.num_features)
        setattr(model, 'patch_size', model.patch_embed.patch_size)
        setattr(model, 'distilled', False)
        model.forward = lambda x, return_features: model.get_intermediate_layers(x, n=1)[0]
    else:
        normalization = model_cfg.pop("normalization")
        model_cfg["n_cls"] = 1000
        mlp_expansion_ratio = 4
        model_cfg["d_ff"] = mlp_expansion_ratio * model_cfg["d_model"]

        if backbone in default_cfgs:
            default_cfg = default_cfgs[backbone]
        else:
            default_cfg = dict(
                pretrained=False,
                num_classes=1000,
                drop_rate=0.0,
                drop_path_rate=0.0,
                drop_block_rate=None,
            )

        default_cfg["input_size"] = (
            3,
            model_cfg["image_size"][0],
            model_cfg["image_size"][1],
        )
        model = VisionTransformer(**model_cfg)
        if backbone == "vit_base_patch8_384":
            path = os.path.expandvars("/home/vobecant/PhD/weights/vit_base_patch8_384.pth")
            state_dict = torch.load(path, map_location="cpu")
            filtered_dict = checkpoint_filter_fn(state_dict, model)
            model.load_state_dict(filtered_dict, strict=True)
        elif "deit" in backbone:
            load_pretrained(model, default_cfg, filter_fn=checkpoint_filter_fn)
        else:
            load_custom_pretrained(model, default_cfg)

    return model


def create_decoder(encoder, decoder_cfg):
    decoder_cfg = decoder_cfg.copy()
    name = decoder_cfg.pop("name")
    decoder_cfg["d_encoder"] = encoder.d_model
    decoder_cfg["patch_size"] = encoder.patch_size

    if "linear" in name:
        decoder = DecoderLinear(**decoder_cfg)
    elif name == "mask_transformer":
        dim = encoder.d_model
        n_heads = dim // 64
        decoder_cfg["n_heads"] = n_heads
        decoder_cfg["d_model"] = dim
        decoder_cfg["d_ff"] = 4 * dim
        decoder = MaskTransformer(**decoder_cfg)
    elif 'deeplab' in name:
        decoder = DeepLabHead(in_channels=encoder.d_model, num_classes=decoder_cfg["n_cls"],
                              patch_size=decoder_cfg["patch_size"])
    else:
        raise ValueError(f"Unknown decoder: {name}")
    return decoder


def create_segmenter(model_cfg):
    model_cfg = model_cfg.copy()
    decoder_cfg = model_cfg.pop("decoder")
    decoder_cfg["n_cls"] = model_cfg["n_cls"]

    if 'weights_path' in model_cfg.keys():
        weights_path = model_cfg.pop('weights_path')
    else:
        weights_path = None

    encoder = create_vit(model_cfg)
    decoder = create_decoder(encoder, decoder_cfg)
    model = Segmenter(encoder, decoder, n_cls=model_cfg["n_cls"])

    if weights_path is not None:
        raise Exception('Wants to load weights to the complete segmenter insice create_segmenter method!')
        state_dict = torch.load(weights_path, map_location="cpu")
        if 'model' in state_dict:
            state_dict = state_dict['model']
        msg = model.load_state_dict(state_dict, strict=False)
        print(msg)

    return model


def load_model(model_path, decoder_only=False, variant_path=None):
    variant_path = Path(model_path).parent / "variant.yml" if variant_path is None else variant_path
    with open(variant_path, "r") as f:
        variant = yaml.load(f, Loader=yaml.FullLoader)
    net_kwargs = variant["net_kwargs"]

    model = create_segmenter(net_kwargs)
    data = torch.load(model_path, map_location=ptu.device)
    checkpoint = data["model"]

    if decoder_only:
        model.decoder.load_state_dict(checkpoint, strict=True)
    else:
        model.load_state_dict(checkpoint, strict=True)

    return model, variant
