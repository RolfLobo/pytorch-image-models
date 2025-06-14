"""VGG

Adapted from https://github.com/pytorch/vision 'vgg.py' (BSD-3-Clause) with a few changes for
timm functionality.

Copyright 2021 Ross Wightman
"""
from typing import Any, Dict, List, Optional, Type, Union, cast

import torch
import torch.nn as nn
import torch.nn.functional as F

from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.layers import ClassifierHead
from ._builder import build_model_with_cfg
from ._features_fx import register_notrace_module
from ._registry import register_model, generate_default_cfgs

__all__ = ['VGG']


cfgs: Dict[str, List[Union[str, int]]] = {
    'vgg11': [64, 'M', 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512, 'M'],
    'vgg13': [64, 64, 'M', 128, 128, 'M', 256, 256, 'M', 512, 512, 'M', 512, 512, 'M'],
    'vgg16': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 'M', 512, 512, 512, 'M', 512, 512, 512, 'M'],
    'vgg19': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 256, 'M', 512, 512, 512, 512, 'M', 512, 512, 512, 512, 'M'],
}


@register_notrace_module  # reason: FX can't symbolically trace control flow in forward method
class ConvMlp(nn.Module):
    """Convolutional MLP block for VGG head.

    Replaces traditional Linear layers with Conv2d layers in the classifier.
    """

    def __init__(
            self,
            in_features: int = 512,
            out_features: int = 4096,
            kernel_size: int = 7,
            mlp_ratio: float = 1.0,
            drop_rate: float = 0.2,
            act_layer: Type[nn.Module] = nn.ReLU,
            conv_layer: Type[nn.Module] = nn.Conv2d,
    ):
        """Initialize ConvMlp.

        Args:
            in_features: Number of input features.
            out_features: Number of output features.
            kernel_size: Kernel size for first conv layer.
            mlp_ratio: Ratio for hidden layer size.
            drop_rate: Dropout rate.
            act_layer: Activation layer type.
            conv_layer: Convolution layer type.
        """
        super(ConvMlp, self).__init__()
        self.input_kernel_size = kernel_size
        mid_features = int(out_features * mlp_ratio)
        self.fc1 = conv_layer(in_features, mid_features, kernel_size, bias=True)
        self.act1 = act_layer(True)
        self.drop = nn.Dropout(drop_rate)
        self.fc2 = conv_layer(mid_features, out_features, 1, bias=True)
        self.act2 = act_layer(True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass.

        Args:
            x: Input tensor.

        Returns:
            Output tensor.
        """
        if x.shape[-2] < self.input_kernel_size or x.shape[-1] < self.input_kernel_size:
            # keep the input size >= 7x7
            output_size = (max(self.input_kernel_size, x.shape[-2]), max(self.input_kernel_size, x.shape[-1]))
            x = F.adaptive_avg_pool2d(x, output_size)
        x = self.fc1(x)
        x = self.act1(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.act2(x)
        return x


class VGG(nn.Module):
    """VGG model architecture.

    Based on `Very Deep Convolutional Networks for Large-Scale Image Recognition`
    - https://arxiv.org/abs/1409.1556
    """

    def __init__(
            self,
            cfg: List[Any],
            num_classes: int = 1000,
            in_chans: int = 3,
            output_stride: int = 32,
            mlp_ratio: float = 1.0,
            act_layer: Type[nn.Module] = nn.ReLU,
            conv_layer: Type[nn.Module] = nn.Conv2d,
            norm_layer: Optional[Type[nn.Module]] = None,
            global_pool: str = 'avg',
            drop_rate: float = 0.,
    ) -> None:
        """Initialize VGG model.

        Args:
            cfg: Configuration list defining network architecture.
            num_classes: Number of classes for classification.
            in_chans: Number of input channels.
            output_stride: Output stride of network.
            mlp_ratio: Ratio for MLP hidden layer size.
            act_layer: Activation layer type.
            conv_layer: Convolution layer type.
            norm_layer: Normalization layer type.
            global_pool: Global pooling type.
            drop_rate: Dropout rate.
        """
        super(VGG, self).__init__()
        assert output_stride == 32
        self.num_classes = num_classes
        self.drop_rate = drop_rate
        self.grad_checkpointing = False
        self.use_norm = norm_layer is not None
        self.feature_info = []

        prev_chs = in_chans
        net_stride = 1
        pool_layer = nn.MaxPool2d
        layers: List[nn.Module] = []
        for v in cfg:
            last_idx = len(layers) - 1
            if v == 'M':
                self.feature_info.append(dict(num_chs=prev_chs, reduction=net_stride, module=f'features.{last_idx}'))
                layers += [pool_layer(kernel_size=2, stride=2)]
                net_stride *= 2
            else:
                v = cast(int, v)
                conv2d = conv_layer(prev_chs, v, kernel_size=3, padding=1)
                if norm_layer is not None:
                    layers += [conv2d, norm_layer(v), act_layer(inplace=True)]
                else:
                    layers += [conv2d, act_layer(inplace=True)]
                prev_chs = v
        self.features = nn.Sequential(*layers)
        self.feature_info.append(dict(num_chs=prev_chs, reduction=net_stride, module=f'features.{len(layers) - 1}'))

        self.num_features = prev_chs
        self.head_hidden_size = 4096
        self.pre_logits = ConvMlp(
            prev_chs,
            self.head_hidden_size,
            7,
            mlp_ratio=mlp_ratio,
            drop_rate=drop_rate,
            act_layer=act_layer,
            conv_layer=conv_layer,
        )
        self.head = ClassifierHead(
            self.head_hidden_size,
            num_classes,
            pool_type=global_pool,
            drop_rate=drop_rate,
        )

        self._initialize_weights()

    @torch.jit.ignore
    def group_matcher(self, coarse: bool = False) -> Dict[str, Any]:
        """Group matcher for parameter groups.

        Args:
            coarse: Whether to use coarse grouping.

        Returns:
            Dictionary of grouped parameters.
        """
        # this treats BN layers as separate groups for bn variants, a lot of effort to fix that
        return dict(stem=r'^features\.0', blocks=r'^features\.(\d+)')

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable: bool = True) -> None:
        """Enable or disable gradient checkpointing.

        Args:
            enable: Whether to enable gradient checkpointing.
        """
        assert not enable, 'gradient checkpointing not supported'

    @torch.jit.ignore
    def get_classifier(self) -> nn.Module:
        """Get the classifier module.

        Returns:
            Classifier module.
        """
        return self.head.fc

    def reset_classifier(self, num_classes: int, global_pool: Optional[str] = None) -> None:
        """Reset the classifier.

        Args:
            num_classes: Number of classes for new classifier.
            global_pool: Global pooling type.
        """
        self.num_classes = num_classes
        self.head.reset(num_classes, global_pool)

    def forward_features(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass through feature extraction layers.

        Args:
            x: Input tensor.

        Returns:
            Feature tensor.
        """
        x = self.features(x)
        return x

    def forward_head(self, x: torch.Tensor, pre_logits: bool = False) -> torch.Tensor:
        """Forward pass through head.

        Args:
            x: Input features.
            pre_logits: Return features before final linear layer.

        Returns:
            Classification logits or features.
        """
        x = self.pre_logits(x)
        return self.head(x, pre_logits=pre_logits) if pre_logits else self.head(x)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass.

        Args:
            x: Input tensor.

        Returns:
            Output logits.
        """
        x = self.forward_features(x)
        x = self.forward_head(x)
        return x

    def _initialize_weights(self) -> None:
        """Initialize model weights."""
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                nn.init.constant_(m.bias, 0)


def _filter_fn(state_dict: dict) -> Dict[str, torch.Tensor]:
    """Convert patch embedding weight from manual patchify + linear proj to conv.

    Args:
        state_dict: State dictionary to filter.

    Returns:
        Filtered state dictionary.
    """
    out_dict = {}
    for k, v in state_dict.items():
        k_r = k
        k_r = k_r.replace('classifier.0', 'pre_logits.fc1')
        k_r = k_r.replace('classifier.3', 'pre_logits.fc2')
        k_r = k_r.replace('classifier.6', 'head.fc')
        if 'classifier.0.weight' in k:
            v = v.reshape(-1, 512, 7, 7)
        if 'classifier.3.weight' in k:
            v = v.reshape(-1, 4096, 1, 1)
        out_dict[k_r] = v
    return out_dict


def _create_vgg(variant: str, pretrained: bool, **kwargs: Any) -> VGG:
    """Create a VGG model.

    Args:
        variant: Model variant name.
        pretrained: Load pretrained weights.
        **kwargs: Additional model arguments.

    Returns:
        VGG model instance.
    """
    cfg = variant.split('_')[0]
    # NOTE: VGG is one of few models with stride==1 features w/ 6 out_indices [0..5]
    out_indices = kwargs.pop('out_indices', (0, 1, 2, 3, 4, 5))
    model = build_model_with_cfg(
        VGG,
        variant,
        pretrained,
        model_cfg=cfgs[cfg],
        feature_cfg=dict(flatten_sequential=True, out_indices=out_indices),
        pretrained_filter_fn=_filter_fn,
        **kwargs,
    )
    return model


def _cfg(url: str = '', **kwargs) -> Dict[str, Any]:
    """Create default configuration dictionary.

    Args:
        url: Model weight URL.
        **kwargs: Additional configuration options.

    Returns:
        Configuration dictionary.
    """
    return {
        'url': url,
        'num_classes': 1000, 'input_size': (3, 224, 224), 'pool_size': (7, 7),
        'crop_pct': 0.875, 'interpolation': 'bilinear',
        'mean': IMAGENET_DEFAULT_MEAN, 'std': IMAGENET_DEFAULT_STD,
        'first_conv': 'features.0', 'classifier': 'head.fc',
        **kwargs
    }


default_cfgs = generate_default_cfgs({
    'vgg11.tv_in1k': _cfg(hf_hub_id='timm/'),
    'vgg13.tv_in1k': _cfg(hf_hub_id='timm/'),
    'vgg16.tv_in1k': _cfg(hf_hub_id='timm/'),
    'vgg19.tv_in1k': _cfg(hf_hub_id='timm/'),
    'vgg11_bn.tv_in1k': _cfg(hf_hub_id='timm/'),
    'vgg13_bn.tv_in1k': _cfg(hf_hub_id='timm/'),
    'vgg16_bn.tv_in1k': _cfg(hf_hub_id='timm/'),
    'vgg19_bn.tv_in1k': _cfg(hf_hub_id='timm/'),
})


@register_model
def vgg11(pretrained: bool = False, **kwargs: Any) -> VGG:
    r"""VGG 11-layer model (configuration "A") from
    `"Very Deep Convolutional Networks For Large-Scale Image Recognition" <https://arxiv.org/pdf/1409.1556.pdf>`._
    """
    model_args = dict(**kwargs)
    return _create_vgg('vgg11', pretrained=pretrained, **model_args)


@register_model
def vgg11_bn(pretrained: bool = False, **kwargs: Any) -> VGG:
    r"""VGG 11-layer model (configuration "A") with batch normalization
    `"Very Deep Convolutional Networks For Large-Scale Image Recognition" <https://arxiv.org/pdf/1409.1556.pdf>`._
    """
    model_args = dict(norm_layer=nn.BatchNorm2d, **kwargs)
    return _create_vgg('vgg11_bn', pretrained=pretrained, **model_args)


@register_model
def vgg13(pretrained: bool = False, **kwargs: Any) -> VGG:
    r"""VGG 13-layer model (configuration "B")
    `"Very Deep Convolutional Networks For Large-Scale Image Recognition" <https://arxiv.org/pdf/1409.1556.pdf>`._
    """
    model_args = dict(**kwargs)
    return _create_vgg('vgg13', pretrained=pretrained, **model_args)


@register_model
def vgg13_bn(pretrained: bool = False, **kwargs: Any) -> VGG:
    r"""VGG 13-layer model (configuration "B") with batch normalization
    `"Very Deep Convolutional Networks For Large-Scale Image Recognition" <https://arxiv.org/pdf/1409.1556.pdf>`._
    """
    model_args = dict(norm_layer=nn.BatchNorm2d, **kwargs)
    return _create_vgg('vgg13_bn', pretrained=pretrained, **model_args)


@register_model
def vgg16(pretrained: bool = False, **kwargs: Any) -> VGG:
    r"""VGG 16-layer model (configuration "D")
    `"Very Deep Convolutional Networks For Large-Scale Image Recognition" <https://arxiv.org/pdf/1409.1556.pdf>`._
    """
    model_args = dict(**kwargs)
    return _create_vgg('vgg16', pretrained=pretrained, **model_args)


@register_model
def vgg16_bn(pretrained: bool = False, **kwargs: Any) -> VGG:
    r"""VGG 16-layer model (configuration "D") with batch normalization
    `"Very Deep Convolutional Networks For Large-Scale Image Recognition" <https://arxiv.org/pdf/1409.1556.pdf>`._
    """
    model_args = dict(norm_layer=nn.BatchNorm2d, **kwargs)
    return _create_vgg('vgg16_bn', pretrained=pretrained, **model_args)


@register_model
def vgg19(pretrained: bool = False, **kwargs: Any) -> VGG:
    r"""VGG 19-layer model (configuration "E")
    `"Very Deep Convolutional Networks For Large-Scale Image Recognition" <https://arxiv.org/pdf/1409.1556.pdf>`._
    """
    model_args = dict(**kwargs)
    return _create_vgg('vgg19', pretrained=pretrained, **model_args)


@register_model
def vgg19_bn(pretrained: bool = False, **kwargs: Any) -> VGG:
    r"""VGG 19-layer model (configuration 'E') with batch normalization
    `"Very Deep Convolutional Networks For Large-Scale Image Recognition" <https://arxiv.org/pdf/1409.1556.pdf>`._
    """
    model_args = dict(norm_layer=nn.BatchNorm2d, **kwargs)
    return _create_vgg('vgg19_bn', pretrained=pretrained, **model_args)
