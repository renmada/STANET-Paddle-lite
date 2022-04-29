import warnings
from typing import Any, Dict, List

import paddle
import paddle.nn as nn
from paddle import Tensor

__all__ = ["MNASNet", "mnasnet0_5", "mnasnet0_75", "mnasnet1_0", "mnasnet1_3"]

_BN_MOMENTUM = 0.9


class _InvertedResidual(nn.Layer):
    def __init__(
            self, in_ch: int, out_ch: int, kernel_size: int, stride: int, expansion_factor: int,
            bn_momentum: float = 0.1
    ) -> None:
        super().__init__()
        assert stride in [1, 2]
        assert kernel_size in [3, 5]
        mid_ch = in_ch * expansion_factor
        self.apply_residual = in_ch == out_ch and stride == 1
        self.layers = nn.Sequential(
            # Pointwise
            nn.Conv2D(in_ch, mid_ch, 1, bias_attr=False),
            nn.BatchNorm2D(mid_ch, momentum=bn_momentum),
            nn.ReLU(),
            # Depthwise
            nn.Conv2D(mid_ch, mid_ch, kernel_size, padding=kernel_size // 2, stride=stride, groups=mid_ch,
                      bias_attr=False),
            nn.BatchNorm2D(mid_ch, momentum=bn_momentum),
            nn.ReLU(),
            # Linear pointwise. Note that there's no activation.
            nn.Conv2D(mid_ch, out_ch, 1, bias_attr=False),
            nn.BatchNorm2D(out_ch, momentum=bn_momentum),
        )

    def forward(self, input: Tensor) -> Tensor:
        if self.apply_residual:
            return self.layers(input) + input
        else:
            return self.layers(input)


def _stack(
        in_ch: int, out_ch: int, kernel_size: int, stride: int, exp_factor: int, repeats: int, bn_momentum: float
) -> nn.Sequential:
    """Creates a stack of inverted residuals."""
    assert repeats >= 1
    # First one has no skip, because feature map size changes.
    first = _InvertedResidual(in_ch, out_ch, kernel_size, stride, exp_factor, bn_momentum=bn_momentum)
    remaining = []
    for _ in range(1, repeats):
        remaining.append(_InvertedResidual(out_ch, out_ch, kernel_size, 1, exp_factor, bn_momentum=bn_momentum))
    return nn.Sequential(first, *remaining)


def _round_to_multiple_of(val: float, divisor: int, round_up_bias: float = 0.9) -> int:
    """Asymmetric rounding to make `val` divisible by `divisor`. With default
    bias, will round up, unless the number is no more than 10% greater than the
    smaller divisible value, i.e. (83, 8) -> 80, but (84, 8) -> 88."""
    assert 0.0 < round_up_bias < 1.0
    new_val = max(divisor, int(val + divisor / 2) // divisor * divisor)
    return new_val if new_val >= round_up_bias * val else new_val + divisor


def _get_depths(alpha: float) -> List[int]:
    """Scales tensor depths as in reference MobileNet code, prefers rouding up
    rather than down."""
    depths = [32, 16, 24, 40, 80, 96, 192, 320]
    return [_round_to_multiple_of(depth * alpha, 8) for depth in depths]


class MNASNet(paddle.nn.Layer):
    _version = 2

    def __init__(self, alpha: float, class_num: int = 1000, dropout: float = 0.0) -> None:
        super().__init__()
        assert alpha > 0.0
        self.alpha = alpha
        self.num_classes = class_num
        depths = _get_depths(alpha)
        layers = [
            # First layer: regular conv.
            nn.Conv2D(3, depths[0], 3, padding=1, stride=2, bias_attr=False),
            nn.BatchNorm2D(depths[0], momentum=_BN_MOMENTUM),
            nn.ReLU(),
            # Depthwise separable, no skip.
            nn.Conv2D(depths[0], depths[0], 3, padding=1, stride=1, groups=depths[0], bias_attr=False),
            nn.BatchNorm2D(depths[0], momentum=_BN_MOMENTUM),
            nn.ReLU(),
            nn.Conv2D(depths[0], depths[1], 1, padding=0, stride=1, bias_attr=False),
            nn.BatchNorm2D(depths[1], momentum=_BN_MOMENTUM),
            nn.ReLU(),
            # MNASNet blocks: stacks of inverted residuals.
            # kernel stride exp_factor repeats
            _stack(depths[1], depths[2], 3, 2, 3, 3, _BN_MOMENTUM),
            _stack(depths[2], depths[3], 5, 2, 3, 3, _BN_MOMENTUM),
            _stack(depths[3], depths[4], 5, 2, 6, 3, _BN_MOMENTUM),
            _stack(depths[4], depths[5], 3, 1, 6, 2, _BN_MOMENTUM),
            _stack(depths[5], depths[6], 5, 2, 6, 4, _BN_MOMENTUM),
            _stack(depths[6], depths[7], 3, 1, 6, 1, _BN_MOMENTUM),
            # Final mapping to classifier input.
            nn.Conv2D(depths[7], 1280, 1, padding=0, stride=1, bias_attr=False),
            nn.BatchNorm2D(1280, momentum=_BN_MOMENTUM),
            nn.ReLU(),
            nn.AdaptiveAvgPool2D(1),
            nn.Flatten()
        ]
        self.layers = nn.Sequential(*layers)
        self.classifier = nn.Linear(1280, class_num)

        # self.classifier = nn.Sequential(nn.Dropout(p=dropout, ), nn.Linear(1280, class_num))
        # self.apply(self._init_weights)

    def forward(self, x: Tensor) -> Tensor:
        x = self.layers(x)
        # Equivalent to global avgpool and removing H and W dimensions.
        # x = x.mean([2, 3])
        return self.classifier(x)

    # def _init_weights(self, layer):
    #     if isinstance(layer, nn.Conv2D):
    #         layer.weight.set_value(paddle.create_parameter(layer.weight.shape,
    #                                                        layer.weight.dtype,
    #                                                        default_initializer=nn.initializer.KaimingNormal()))
    #     elif isinstance(layer, nn.Linear):
    #         layer.weight.set_value(paddle.create_parameter(layer.weight.shape,
    #                                                        layer.weight.dtype,
    #                                                        default_initializer=nn.initializer.KaimingNormal()))


def mnasnet0_5(pretrained: bool = False, progress: bool = True, **kwargs: Any) -> MNASNet:
    r"""MNASNet with depth multiplier of 0.5 from
    `"MnasNet: Platform-Aware Neural Architecture Search for Mobile"
    <https://arxiv.org/pdf/1807.11626.pdf>`_.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = MNASNet(0.5, **kwargs)
    return model


def mnasnet0_75(pretrained: bool = False, progress: bool = True, **kwargs: Any) -> MNASNet:
    r"""MNASNet with depth multiplier of 0.75 from
    `"MnasNet: Platform-Aware Neural Architecture Search for Mobile"
    <https://arxiv.org/pdf/1807.11626.pdf>`_.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = MNASNet(0.75, **kwargs)

    return model


def mnasnet1_0(pretrained: bool = False, progress: bool = True, **kwargs: Any) -> MNASNet:
    r"""MNASNet with depth multiplier of 1.0 from
    `"MnasNet: Platform-Aware Neural Architecture Search for Mobile"
    <https://arxiv.org/pdf/1807.11626.pdf>`_.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = MNASNet(1.0, **kwargs)

    return model


def mnasnet1_3(pretrained: bool = False, progress: bool = True, **kwargs: Any) -> MNASNet:
    r"""MNASNet with depth multiplier of 1.3 from
    `"MnasNet: Platform-Aware Neural Architecture Search for Mobile"
    <https://arxiv.org/pdf/1807.11626.pdf>`_.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = MNASNet(1.3, **kwargs)
    return model


if __name__ == "__main__":
    model = mnasnet1_0()
    # paddle.summary(model, (1, 3, 224, 224
    #                        ))
    # print(model)
    net = model
    d = {}
    for k, v in net.named_parameters():
        d[k] = paddle.full_like(v, 0.02)
    net.set_state_dict(d)
    net.eval()
    inp = paddle.full([1, 3, 224, 224], 0.01)
    print(net(inp))
    paddle.summary(model, (1, 3, 224, 224
                           ))
    # print(net.features[0])