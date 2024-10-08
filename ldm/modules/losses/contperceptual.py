import torch
import torch.nn as nn

from dfs.third_party.taming_transformers.taming.modules.losses.vqperceptual import (
    LPIPS,
    NLayerDiscriminator,
    adopt_weight,
    hinge_d_loss,
    vanilla_d_loss,
    weights_init,
)


class LPIPSWithDiscriminator(nn.Module):
    def __init__(
        self,
        disc_start,
        logvar_init=0.0,
        kl_weight=1.0,
        pixelloss_weight=1.0,
        disc_num_layers=3,
        disc_in_channels=3,
        disc_factor=1.0,
        disc_weight=1.0,
        perceptual_weight=1.0,
        use_actnorm=False,
        disc_conditional=False,
        disc_loss="hinge",
    ):
        super().__init__()
        assert disc_loss in ["hinge", "vanilla"]
        self.kl_weight = kl_weight
        self.pixel_weight = pixelloss_weight
        self.perceptual_loss = LPIPS().eval()
        self.perceptual_weight = perceptual_weight
        # output log variance
        self.logvar = nn.Parameter(torch.ones(size=(1,)) * logvar_init, requires_grad=True)

        self.discriminator = NLayerDiscriminator(
            input_nc=disc_in_channels, n_layers=disc_num_layers, use_actnorm=use_actnorm
        ).apply(weights_init)
        self.discriminator_iter_start = disc_start
        self.disc_loss = hinge_d_loss if disc_loss == "hinge" else vanilla_d_loss
        self.disc_factor = disc_factor
        self.discriminator_weight = disc_weight
        self.disc_conditional = disc_conditional

    def calculate_adaptive_weight(self, nll_loss, g_loss, last_layer=None):
        if last_layer is not None:
            nll_grads = torch.autograd.grad(nll_loss, last_layer, retain_graph=True)[0]
            g_grads = torch.autograd.grad(g_loss, last_layer, retain_graph=True)[0]
        else:
            nll_grads = torch.autograd.grad(nll_loss, self.last_layer[0], retain_graph=True)[0]
            g_grads = torch.autograd.grad(g_loss, self.last_layer[0], retain_graph=True)[0]

        d_weight = torch.norm(nll_grads) / (torch.norm(g_grads) + 1e-4)
        d_weight = torch.clamp(d_weight, 0.0, 1e4).detach()
        d_weight = d_weight * self.discriminator_weight
        return d_weight

    def forward(
        self,
        inputs,
        reconstructions,
        posteriors,
        global_step,
        last_layer=None,
        cond=None,
        weights=None,
    ):
        pix_rec_loss = torch.abs(inputs.contiguous() - reconstructions.contiguous())
        if self.perceptual_weight > 0:
            p_loss = self.perceptual_loss(inputs.contiguous(), reconstructions.contiguous())
        else:
            p_loss = torch.tensor(0.0)
        rec_loss = self.pixel_weight * pix_rec_loss + self.perceptual_weight * p_loss

        nll_loss = rec_loss / torch.exp(self.logvar) + self.logvar
        weighted_nll_loss = nll_loss
        if weights is not None:
            weighted_nll_loss = weights * nll_loss
        weighted_nll_loss = torch.mean(weighted_nll_loss)
        nll_loss = torch.mean(nll_loss)
        kl_loss = posteriors.kl()
        kl_loss = torch.mean(kl_loss)

        # now the GAN part

        # generator update
        if cond is None:
            assert not self.disc_conditional
            logits_fake = self.discriminator(reconstructions.contiguous())
        else:
            assert self.disc_conditional
            logits_fake = self.discriminator(torch.cat((reconstructions.contiguous(), cond), dim=1))
        g_loss = -torch.mean(logits_fake)

        if self.disc_factor > 0.0:
            try:
                d_weight = self.calculate_adaptive_weight(nll_loss, g_loss, last_layer=last_layer)
            except RuntimeError:
                assert not self.training
                d_weight = torch.tensor(0.0, device=kl_loss.device)
        else:
            d_weight = torch.tensor(0.0, device=kl_loss.device)

        disc_factor = adopt_weight(self.disc_factor, global_step, threshold=self.discriminator_iter_start)
        ae_loss = weighted_nll_loss + self.kl_weight * kl_loss + d_weight * disc_factor * g_loss

        log_ae = {
            "ae_loss": ae_loss.clone().detach().mean(),
            "logvar": self.logvar.detach(),
            "kl_loss": kl_loss.detach().mean(),
            "nll_loss": nll_loss.detach().mean(),
            "rec_loss": rec_loss.detach().mean(),
            "pix_rec_loss": pix_rec_loss.detach().mean(),
            "perceptual_loss": p_loss.detach().mean(),
            "d_weight": d_weight.detach(),
            "disc_factor": torch.tensor(disc_factor, device=ae_loss.device),
            "g_loss": g_loss.detach().mean(),
        }

        # second pass for discriminator update
        if cond is None:
            logits_real = self.discriminator(inputs.contiguous().detach())
            logits_fake = self.discriminator(reconstructions.contiguous().detach())
        else:
            logits_real = self.discriminator(torch.cat((inputs.contiguous().detach(), cond), dim=1))
            logits_fake = self.discriminator(torch.cat((reconstructions.contiguous().detach(), cond), dim=1))

        disc_factor = adopt_weight(self.disc_factor, global_step, threshold=self.discriminator_iter_start)
        d_loss = disc_factor * self.disc_loss(logits_real, logits_fake)

        log_disc = {
            "disc_loss": d_loss.clone().detach().mean(),
            "logits_real": logits_real.detach().mean(),
            "logits_fake": logits_fake.detach().mean(),
        }

        return ae_loss, d_loss, log_ae, log_disc
