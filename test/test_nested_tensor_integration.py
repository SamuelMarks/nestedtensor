import traceback
import functools
import pdb
import sys
import torch
import nestedtensor
import unittest
from utils import TestCase
import random
import utils
import torchvision
from torchvision.models._utils import IntermediateLayerGetter
from frozen_batch_norm_2d import NTFrozenBatchNorm2d


class ConfusionMatrix(object):
    def __init__(self, num_classes):
        self.num_classes = num_classes
        self.mat = None

    def update(self, a, b):
        n = self.num_classes
        if self.mat is None:
            self.mat = torch.zeros((n, n), dtype=torch.int64, device=a.device)
        with torch.no_grad():
            k = (a >= 0) & (a < n)
            inds = n * a[k].to(torch.int64) + b[k]
            self.mat += torch.bincount(inds, minlength=n ** 2).reshape(n, n)

    def compute(self):
        h = self.mat.float()
        acc_global = torch.diag(h).sum() / h.sum()
        acc = torch.diag(h) / h.sum(1)
        iu = torch.diag(h) / (h.sum(1) + h.sum(0) - torch.diag(h))
        return acc_global, acc, iu

    def reduce_from_all_processes(self):
        if not torch.distributed.is_available():
            return
        if not torch.distributed.is_initialized():
            return
        torch.distributed.barrier()
        torch.distributed.all_reduce(self.mat)

    def __str__(self):
        acc_global, acc, iu = self.compute()
        return (
            "global correct: {:.1f}\n"
            "average row correct: {}\n"
            "IoU: {}\n"
            "mean IoU: {:.1f}"
        ).format(
            acc_global.item() * 100,
            ["{:.1f}".format(i) for i in (acc * 100).tolist()],
            ["{:.1f}".format(i) for i in (iu * 100).tolist()],
            iu.mean().item() * 100,
        )


class TestIntegration(TestCase):
    # @unittest.skipIf(
    #     not utils.internet_on(), "Cannot reach internet to download reference model."
    # )
    @unittest.skip("Not supported")
    def test_segmentation_pretrained_test_only(self):

        def _test(seed, model_factory, use_confmat, num_classes=21):
            torch.manual_seed(seed)
            t1 = torch.randn(3, 3, 4, requires_grad=True)
            t2 = torch.randn(3, 3, 4, requires_grad=True)
            tr1 = torch.randn(3, 4, requires_grad=True)
            tr2 = torch.randn(3, 4, requires_grad=True)

            model1 = model_factory()
            # model1 = torchvision.models.segmentation.__dict__[model_name](
            #     num_classes=num_classes, aux_loss=aux_loss, pretrained=True
            # )
            # model1.eval()

            # tensor run
            t_input = torch.stack([t1, t2])
            t_target = torch.stack([tr1, tr2])
            if use_confmat:
                confmat = ConfusionMatrix(num_classes)

            output1 = model1(t_input)
            if use_confmat:
                output1 = output1["out"]
            else:
                output1 = output1["0"]

            if use_confmat:
                confmat.update(t_target.flatten(), output1.argmax(1).flatten())
                confmat.reduce_from_all_processes()

            # nt run
            # model2 = torchvision.models.segmentation.__dict__[model_name](
            #     num_classes=num_classes, aux_loss=aux_loss, pretrained=True
            # )
            # model2.eval()
            model2 = model_factory()
            nt_t1 = t1.clone().detach()
            nt_t2 = t2.clone().detach()
            nt_tr1 = tr1.clone().detach()
            nt_tr2 = tr2.clone().detach()

            nt_input = nestedtensor.nested_tensor(
                [nt_t1, nt_t2], requires_grad=True)
            nt_target = nestedtensor.nested_tensor(
                [nt_tr1, nt_tr2], requires_grad=True)
            if use_confmat:
                confmat2 = ConfusionMatrix(num_classes)

            output2 = model2(nt_input)
            if use_confmat:
                output2 = output2["out"]
            else:
                output2 = output2["0"]

            if use_confmat:
                for a, b in zip(nt_target, output2):
                    confmat2.update(a.flatten(), b.argmax(0).flatten())

            if use_confmat:
                confmat2.reduce_from_all_processes()
                self.assertEqual(confmat.mat, confmat2.mat)

            # grad test
            output1_sum = output1.sum()
            output2_sum = output2.sum()
            self.assertEqual(output1_sum, output2_sum)

            output1_sum.backward()
            output2_sum.backward()

            for (n1, p1), (n2, p2) in zip(model1.named_parameters(), model2.named_parameters()):
                if p1.grad is not None:
                    self.assertEqual(p1.grad, p2.grad)
                else:
                    self.assertIsNone(p2.grad)

            # TODO: Re-enable under autograd support
            self.assertEqual(t1.grad, nt_input.grad[0])
            self.assertEqual(t2.grad, nt_input.grad[1])

        _test(1010, lambda: torchvision.models.segmentation.__dict__["fcn_resnet101"](
            num_classes=21, aux_loss="store_true", pretrained=True
        ).eval(), True)

        # _test(10, lambda: IntermediateLayerGetter(getattr(torchvision.models, "resnet18")(
        #     replace_stride_with_dilation=[False, False, False],
        #     pretrained=True, norm_layer=FrozenBatchNorm2d), {'layer4': "0"}), False)


if __name__ == "__main__":
    unittest.main()
