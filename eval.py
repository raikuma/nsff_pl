from argparse import ArgumentParser
import cv2
from collections import defaultdict
import imageio
import numpy as np
import os
from PIL import Image
import torch
from tqdm import tqdm

from models.rendering import render_rays
from models.nerf import PosEmbedding, NeRF

from utils import load_ckpt, visualize_depth
import metrics

from datasets import dataset_dict
from datasets.depth_utils import *
from datasets.ray_utils import *

torch.backends.cudnn.benchmark = True

def get_opts():
    parser = ArgumentParser()
    parser.add_argument('--root_dir', type=str, required=True,
                        help='root directory of dataset')
    parser.add_argument('--dataset_name', type=str, default='monocular',
                        choices=['monocular'],
                        help='which dataset to validate')
    parser.add_argument('--scene_name', type=str, default='test',
                        help='scene name, used as output folder name')
    parser.add_argument('--split', type=str, default='test',
                        help='test or test_spiral')
    parser.add_argument('--img_wh', nargs="+", type=int, default=[800, 800],
                        help='resolution (img_w, img_h) of the image')
    parser.add_argument('--start_end', nargs="+", type=int, default=[0, -1],
                        help='start frame and end frame')

    parser.add_argument('--N_samples', type=int, default=64,
                        help='number of coarse samples')
    parser.add_argument('--N_importance', type=int, default=128,
                        help='number of additional fine samples')
    parser.add_argument('--chunk', type=int, default=32*1024,
                        help='chunk size to split the input to avoid OOM')

    # NeRF-W parameters
    parser.add_argument('--N_vocab', type=int, default=100,
                        help='''number of vocabulary (number of images) 
                                in the dataset for nn.Embedding''')
    parser.add_argument('--encode_a', default=False, action="store_true",
                        help='whether to encode appearance (NeRF-A)')
    parser.add_argument('--N_a', type=int, default=48,
                        help='number of embeddings for appearance')
    parser.add_argument('--encode_t', default=False, action="store_true",
                        help='whether to encode transient object (NeRF-U)')
    parser.add_argument('--N_tau', type=int, default=48,
                        help='number of embeddings for transient objects')
    parser.add_argument('--flow_scale', type=float, default=0.2,
                        help='flow scale to multiply to flow network output')
    parser.add_argument('--output_transient', default=False, action="store_true",
                        help='whether to output the full result (static+transient)')

    parser.add_argument('--ckpt_path', type=str, required=True,
                        help='pretrained checkpoint path to load')

    parser.add_argument('--video_format', type=str, default='mp4',
                        choices=['mp4', 'gif'],
                        help='which format to save')
    parser.add_argument('--fps', type=int, default=10,
                        help='video frame per second')

    parser.add_argument('--save_depth', default=False, action="store_true",
                        help='whether to save depth prediction')
    parser.add_argument('--depth_format', type=str, default='pfm',
                        choices=['png', 'pfm', 'bytes'],
                        help='which format to save')

    parser.add_argument('--save_error', default=False, action="store_true",
                        help='whether to save error map')

    return parser.parse_args()


@torch.no_grad()
def batched_inference(models, embeddings,
                      rays, ts, max_t, N_samples, N_importance,
                      chunk,
                      **kwargs):
    """Do batched inference on rays using chunk."""
    B = rays.shape[0]
    results = defaultdict(list)
    for i in range(0, B, chunk):
        rendered_ray_chunks = \
            render_rays(models,
                        embeddings,
                        rays[i:i+chunk],
                        None if ts is None else ts[i:i+chunk],
                        max_t,
                        N_samples,
                        0,
                        0,
                        N_importance,
                        chunk,
                        test_time=True,
                        **kwargs)

        for k, v in rendered_ray_chunks.items():
            results[k] += [v.cpu()]
    for k, v in results.items():
        results[k] = torch.cat(v, 0)
    return results


if __name__ == "__main__":
    args = get_opts()
    w, h = args.img_wh

    kwargs = {'root_dir': args.root_dir,
              'split': args.split,
              'img_wh': (w, h)}
    if args.dataset_name == 'monocular':
        kwargs['start_end'] = tuple(args.start_end)
    dataset = dataset_dict[args.dataset_name](**kwargs)

    dir_name = f'results/{args.dataset_name}/{args.scene_name}'
    os.makedirs(dir_name, exist_ok=True)

    kwargs = {'K': dataset.K, 'dataset': dataset}

    # main process for merging BlenderProc and NeRF
    embedding_xyz = PosEmbedding(9, 10)
    embedding_dir = PosEmbedding(3, 4)
    embeddings = {'xyz': embedding_xyz,
                  'dir': embedding_dir}

    if args.encode_a:
        embedding_a = torch.nn.Embedding(args.N_vocab, args.N_a).cuda()
        embeddings['a'] = embedding_a
        load_ckpt(embedding_a, args.ckpt_path, 'embedding_a')
    if args.encode_t:
        embedding_t = torch.nn.Embedding(args.N_vocab, args.N_tau).cuda()
        embeddings['t'] = embedding_t
        load_ckpt(embedding_t, args.ckpt_path, 'embedding_t')

    nerf_fine = NeRF(typ='fine',
                     encode_appearance=args.encode_a,
                     in_channels_a=args.N_a,
                     encode_transient=args.encode_t,
                     in_channels_t=args.N_tau,
                     flow_scale=args.flow_scale).cuda()
    load_ckpt(nerf_fine, args.ckpt_path, model_name='nerf_fine')
    models = {'fine': nerf_fine}
    if args.N_importance > 0:
        nerf_coarse = NeRF(typ='coarse',
                           encode_transient=args.encode_t,
                           in_channels_t=args.N_tau).cuda()
        load_ckpt(nerf_coarse, args.ckpt_path, model_name='nerf_coarse')
        models['coarse'] = nerf_coarse
    
    kwargs['output_transient'] = args.output_transient
    kwargs['output_transient_flow'] = []

    imgs, psnrs = [], []

    for i in tqdm(range(len(dataset))):
        sample = dataset[i]

        directions = get_ray_directions(h, w, dataset.K)
        c2w = sample['c2w']
        rays_o_r, rays_d_r = get_rays(directions, c2w)
        shift_near = -min(-1.0, c2w[2, 3])
        rays_o, rays_d = get_ndc_rays(dataset.K, 1.0,
                                      shift_near, rays_o_r, rays_d_r)

        ts = None if 'ts' not in sample else sample['ts'].cuda()
        results = batched_inference(models, embeddings, sample['rays'].cuda(), ts,
                                    dataset.N_frames-1, args.N_samples, args.N_importance,
                                    args.chunk, **kwargs)
        img_pred = np.clip(results['rgb_fine'].view(h, w, 3).numpy(), 0, 1)
        
        if args.save_depth:
            depth_pred = results['depth_fine'].view(h, w).numpy()
            depth_pred = np.nan_to_num(depth_pred)
            if args.depth_format == 'png':
                depth_pred_ = visualize_depth(torch.from_numpy(depth_pred)).permute(1, 2, 0).numpy()
                depth_pred_img = (depth_pred_*255).astype(np.uint8)
                imageio.imwrite(os.path.join(dir_name, f'depth_{i:03d}.png'), depth_pred_img)
            elif args.depth_format == 'pfm':
                save_pfm(os.path.join(dir_name, f'depth_{i:03d}.pfm'), depth_pred)
            elif args.depth_format == 'bytes':
                with open(f'depth_{i:03d}', 'wb') as f:
                    f.write(depth_pred.tobytes())

        img_pred_ = (img_pred*255).astype(np.uint8)
        imgs += [img_pred_]
        imageio.imwrite(os.path.join(dir_name, f'{i:03d}.png'), img_pred_)

        if 'rgbs' in sample:
            rgbs = sample['rgbs']
            img_gt = rgbs.view(h, w, 3)
            psnrs += [metrics.psnr(img_gt, img_pred).item()]

            if args.save_error:
                err = torch.abs(img_gt-img_pred).sum(-1).numpy()
                err = (err-err.min())/(err.max()-err.min())
                err_vis = cv2.applyColorMap((err*255).astype(np.uint8), cv2.COLORMAP_JET)
                cv2.imwrite(os.path.join(dir_name, f'err_{i:03d}.png'), err_vis)
        
    if psnrs:
        mean_psnr = np.mean(psnrs)
        print(f'Mean PSNR : {mean_psnr:.2f}')
        np.save(os.path.join(dir_name, 'psnr.npy'), np.array(psnrs))

    imageio.mimsave(os.path.join(dir_name, f'{args.scene_name}.{args.video_format}'),
                    imgs, fps=args.fps)