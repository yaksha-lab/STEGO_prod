import io

import PIL.Image
import matplotlib.pyplot as plt
import torch
from tensorboardX import SummaryWriter
from torch.nn import Sequential, Linear, LogSoftmax
from torch.utils.data import Dataset
from torchvision.transforms import ToTensor
from utils import *
from modules import *
from data import *
from tqdm import tqdm
from torch.utils.data import DataLoader
import numpy as np
from skimage.segmentation import mark_boundaries
from sklearn.decomposition import PCA
from kornia.color import rgb_to_lab
from datetime import datetime
import hydra
from omegaconf import DictConfig, OmegaConf

def norm(t):
    return F.normalize(t, dim=1, eps=1e-10)

def prep(continuous: bool, t: torch.Tensor):
    if continuous:
        return norm(t)
    else:
        return torch.exp(t)

def entropy(p):
    p = torch.clamp_min(p, .0000001)
    return -(p * torch.log(p)).sum(dim=1)


@hydra.main(config_path="configs", config_name="train_config.yml")
def my_app(cfg: DictConfig) -> None:
    OmegaConf.set_struct(cfg, False)
    print(OmegaConf.to_yaml(cfg))
    pytorch_data_dir = cfg.pytorch_data_dir
    log_dir = join(cfg.output_root, "logs")
    continuous = cfg.continuous
    dim = cfg.dim
    dataset_name = cfg.dataset_name
    n_images = cfg.n_images
    chosen_imageset = 'train'
    
    np.random.seed(0)
    torch.random.manual_seed(0)

    small_imsize = cfg.res // 2
    transform_with_resize = T.Compose([T.Resize((small_imsize, small_imsize)), T.ToTensor(), normalize])
    label_transform_with_resize = T.Compose([T.Resize((small_imsize, small_imsize)), ToTargetTensor()])
    
    # dataset = ContrastiveSegDataset(
    #     pytorch_data_dir, dataset_name, "train+val", cfg.num_neighbors,
    #     transform_with_resize, label_transform_with_resize, None, None, cfg=cfg, num_neighbors=cfg.num_neighbors)
    
    dataset = ContrastiveSegDataset(
        pytorch_data_dir, 
        dataset_name,
        crop_type=cfg.crop_type,
        image_set=chosen_imageset,
        transform=transform_with_resize, 
        target_transform=label_transform_with_resize, 
        cfg=cfg)
    
    # def __init__(self,
    #              pytorch_data_dir,
    #              dataset_name,
    #              crop_type,
    #              image_set,
    #              transform,
    #              target_transform,
    #              cfg,
    #              aug_geometric_transform=None,
    #              aug_photometric_transform=None,
    #              num_neighbors=5,
    #              compute_knns=False,
    #              mask=False,
    #              pos_labels=False,
    #              pos_images=False,
    #              extra_transform=None,
    #              model_type_override=None
    #              ):
    
    prefix = "crf/{}_{}".format(cfg.dataset_name, cfg.experiment_name)
    writer = SummaryWriter(
        join(log_dir, '{}_date_{}'.format(prefix, datetime.now().strftime("%m:%d:%Y:%H:%M"))))

    class CodeSpaceTable(torch.nn.Module):
        def __init__(self, continuous, n_images, dim, h, w):
            super(CodeSpaceTable, self).__init__()
            self.continuous = continuous
            self.code_space = torch.nn.Parameter(torch.randn(n_images, dim, h, w) * .1)

        def forward(self, x):
            if self.continuous:
                return self.code_space
            else:
                return torch.nn.functional.log_softmax(self.code_space, 1)

    def add_plot(writer, name, step):
        buf = io.BytesIO()
        plt.savefig(buf, format='jpeg')
        buf.seek(0)
        image = PIL.Image.open(buf)
        image = ToTensor()(image)
        writer.add_image(name, image, step)
        plt.clf()
        plt.close()

    loader = DataLoader(dataset, n_images, shuffle=False, num_workers=0)

    load_iter = iter(loader)
    for i in range(1):
        next(load_iter)
    pack = next(load_iter)
    pack = {k: v.cuda(non_blocking=True) for k, v in pack.items()}
    ind = pack["ind"]
    img = pack["img"]

    net = CodeSpaceTable(continuous, n_images, dim, img.shape[2], img.shape[3]).cuda()
    optim = torch.optim.Adam(list(net.parameters()), lr=1e-2)

    loss_func = ContrastiveCRFLoss(cfg.crf_samples, cfg.alpha, cfg.beta, cfg.gamma, cfg.w1, cfg.w2, cfg.shift)

    def to_normed_lab(img):
        img_t = rgb_to_lab(img)
        img_t /= torch.tensor([100, 128 * 2, 128 * 2]).unsqueeze(0).unsqueeze(-1).unsqueeze(-1).cuda()
        return img_t

    for i in tqdm(range(cfg.epochs)):

        code = net.forward(img)
        if cfg.color_space == "rgb":
            img_t = img
        elif cfg.color_space == "lab":
            img_t = to_normed_lab(img)
        else:
            raise ValueError("unknown color space: {}".format(cfg.color_space))

        if continuous:
            ent_reg_term = 0
        else:
            ent_global = entropy(torch.exp(code).mean(dim=0, keepdim=True)).mean()
            ent_local = entropy(torch.exp(code)).mean()
            ent_reg_term = - cfg.global_ent_weight * ent_global \
                           - cfg.local_ent_weight * ent_local

            if i % 100 == 0:
                writer.add_scalar('ent/ent1', ent_global, i)
                writer.add_scalar('ent/ent2', ent_local, i)

        crf_loss = loss_func(img_t, prep(continuous, code))
        loss = crf_loss.mean() + ent_reg_term

        loss.backward()
        optim.step()
        optim.zero_grad()

        if i % 10 == 0:
            writer.add_scalar("crf_loss", crf_loss.mean(), i)
            writer.add_scalar("loss", loss, i)

        if i % 500 == 0:
            fig, ax = plt.subplots(2, n_images, figsize=(n_images * 3, 2 * 3))
            with torch.no_grad():
                for idx, img_idx in enumerate(ind[:n_images]):
                    plot_img = unnorm(img)[idx].permute(1, 2, 0)
                    plot_img = (plot_img - plot_img.min()) / (plot_img.max() - plot_img.min())
                    ax[0, idx].imshow(plot_img.cpu())
                    if not continuous:
                        ax[1, idx].imshow(mark_boundaries(plot_img.cpu(), code.argmax(1)[idx].cpu().numpy()))
                    else:
                        X_code = code[idx].permute(1, 2, 0).reshape(-1, dim).cpu()
                        projected_code = PCA(n_components=3).fit_transform(X_code) \
                            .reshape([code.shape[2], code.shape[3], 3])
                        projected_code = (projected_code + 1) / 2
                        projected_code = np.clip(projected_code, 0, 1)
                        ax[1, idx].imshow(projected_code)

                remove_axes(ax)
                plt.tight_layout()
                add_plot(writer, "plot", i)


if __name__ == "__main__":
    my_app()
