import argparse, json, os, time, math
import numpy as np, cv2, torch, torch.nn.functional as Fn
p = argparse.ArgumentParser()
p.add_argument('--map', default='office7'); p.add_argument('--name', required=True)
p.add_argument('--steps', type=int, default=3000)
p.add_argument('--init', default='depth', choices=['depth', 'sparse', 'random'])
p.add_argument('--voxel', type=float, default=0.03)       # depth-init voxel (m)
p.add_argument('--sparse_n', type=int, default=20000)
p.add_argument('--random_n', type=int, default=100000)
p.add_argument('--sh', type=int, default=0)
p.add_argument('--densify', type=int, default=1)
p.add_argument('--depth_w', type=float, default=0.0)
p.add_argument('--split', default='every8', choices=['every8', 'block', 'all'])
p.add_argument('--scale', type=float, default=1.0)         # image downscale
p.add_argument('--mem_frac', type=float, default=0.3)
p.add_argument('--seed', type=int, default=0)
p.add_argument('--max_gs', type=int, default=0)             # cap on gaussians (0 = none)
p.add_argument('--ssim_w', type=float, default=0.2)
p.add_argument('--grow_grad', type=float, default=0.0002)
p.add_argument('--refine_stop_frac', type=float, default=0.6)
p.add_argument('--absgrad', type=int, default=0)
p.add_argument('--lr_means', type=float, default=1.6e-4)
p.add_argument('--scale_reg',type=float,default=0.0)
p.add_argument('--expo',type=int,default=0)
p.add_argument('--no_eval_lpips', action='store_true')
a = p.parse_args()
torch.manual_seed(a.seed); np.random.seed(a.seed)
dev = 'cuda'; torch.cuda.set_per_process_memory_fraction(a.mem_frac)
from gsplat import rasterization
from gsplat.strategy import DefaultStrategy
out = f'out/{a.name}'; os.makedirs(out, exist_ok=True)
F = json.load(open(f'data/{a.map}/frames.json')); N = len(F)
if a.split == 'every8': test = set(range(3, N, 8))
elif a.split == 'block': test = set(range(N // 2, N // 2 + 12))
else: test = set()
train = [i for i in range(N) if i not in test]; test = sorted(test)
t_load = time.time()
def load(i):
    f = F[i]; im = cv2.cvtColor(cv2.imread(f'data/{a.map}/images/{f["id"]}.jpg'), cv2.COLOR_BGR2RGB)
    D = cv2.imread(f'data/{a.map}/depth/{f["id"]}.png', -1).astype(np.float32) * f['depth_scale']
    K = np.array([[f['fx'], 0, f['cx']], [0, f['fy'], f['cy']], [0, 0, 1]], np.float32)
    if a.scale != 1.0:
        W, H = int(round(im.shape[1] * a.scale)), int(round(im.shape[0] * a.scale))
        im = cv2.resize(im, (W, H), interpolation=cv2.INTER_AREA)
        D = cv2.resize(D, (W, H), interpolation=cv2.INTER_NEAREST); K[:2] *= a.scale
    lo, hi = f['valid_range']; D[(D < lo) | (D > hi)] = 0
    return im, D, K, np.array(f['c2w'], np.float32)
data = [load(i) for i in range(N)]
H, W = data[0][0].shape[:2]
img = torch.from_numpy(np.stack([d[0] for d in data])).float().div(255).to(dev)       # N,H,W,3
dep = torch.from_numpy(np.stack([d[1] for d in data])).to(dev)
Ks = torch.from_numpy(np.stack([d[2] for d in data])).to(dev)
c2w = torch.from_numpy(np.stack([d[3] for d in data])).to(dev)
w2c = torch.linalg.inv(c2w)
t_load = time.time() - t_load
cpos = c2w[:, :3, 3]; center = cpos[train].mean(0)
scene_scale = float((cpos[train] - center).norm(dim=1).max()) * 1.1
# ---- init
t_init = time.time()
def backproject(i, stride=1):
    D = dep[i]; v = D > 0
    if stride > 1:
        m = torch.zeros_like(v); m[::stride, ::stride] = True; v = v & m
    ys, xs = torch.nonzero(v, as_tuple=True); d = D[ys, xs]; K = Ks[i]
    pc = torch.stack([(xs - K[0, 2]) * d / K[0, 0], (ys - K[1, 2]) * d / K[1, 1], d], 1)
    pw = pc @ c2w[i, :3, :3].T + c2w[i, :3, 3]
    return pw, img[i][ys, xs]
if a.init in ('depth', 'sparse'):
    P, C = zip(*[backproject(i) for i in train]); P = torch.cat(P); C = torch.cat(C)
    raw_pts = len(P)
    key = torch.floor(P / a.voxel).long(); key = key - key.min(0).values
    ext = key.max(0).values + 1
    lin = (key[:, 0] * ext[1] + key[:, 1]) * ext[2] + key[:, 2]
    uniq, inv = torch.unique(lin, return_inverse=True)
    cnt = torch.zeros(len(uniq), device=dev).index_add_(0, inv, torch.ones_like(lin, dtype=torch.float))
    Pm = torch.zeros(len(uniq), 3, device=dev).index_add_(0, inv, P) / cnt[:, None]
    Cm = torch.zeros(len(uniq), 3, device=dev).index_add_(0, inv, C) / cnt[:, None]
    if a.init == 'sparse' and len(Pm) > a.sparse_n:
        sel = torch.randperm(len(Pm), device=dev)[:a.sparse_n]; Pm, Cm = Pm[sel], Cm[sel]
    pts, cols = Pm, Cm
else:
    raw_pts = 0
    lo = cpos[train].min(0).values - 2.0; hi = cpos[train].max(0).values + 2.0
    pts = lo + torch.rand(a.random_n, 3, device=dev) * (hi - lo); cols = torch.rand(a.random_n, 3, device=dev)
Ng = len(pts)
from scipy.spatial import cKDTree
dist = torch.from_numpy(cKDTree(pts.cpu().numpy()).query(pts.cpu().numpy(), k=4)[0][:, 1:].mean(1)).float().to(dev)
dist = dist.clamp(1e-3, 1.0)
t_init = time.time() - t_init
SH0 = 0.28209479177387814
def rgb2sh(c): return (c - 0.5) / SH0
params = {
    'means': pts.clone(), 'scales': torch.log(dist * 1.0)[:, None].repeat(1, 3),
    'quats': Fn.normalize(torch.randn(Ng, 4, device=dev), dim=-1),
    'opacities': torch.logit(torch.full((Ng,), 0.3, device=dev)),
    'sh0': rgb2sh(cols)[:, None, :], 'shN': torch.zeros(Ng, (a.sh + 1) ** 2 - 1, 3, device=dev)}
params = {k: torch.nn.Parameter(v) for k, v in params.items()}
lrs = {'means': a.lr_means * scene_scale, 'scales': 5e-3, 'quats': 1e-3, 'opacities': 5e-2, 'sh0': 2.5e-3, 'shN': 2.5e-3 / 20}
opts = {k: torch.optim.Adam([params[k]], lr=lrs[k], eps=1e-15) for k in params}
sched = torch.optim.lr_scheduler.ExponentialLR(opts['means'], gamma=0.01 ** (1.0 / a.steps))
strategy = DefaultStrategy(verbose=False, refine_start_iter=300, refine_stop_iter=int(a.steps * a.refine_stop_frac),
                           refine_every=100, reset_every=max(a.steps // 3, 1000), grow_grad2d=a.grow_grad,
                           absgrad=bool(a.absgrad), revised_opacity=True) if a.densify else None
gain=torch.nn.Parameter(torch.zeros(N,3,device=dev)); bias=torch.nn.Parameter(torch.zeros(N,3,device=dev)); eopt=torch.optim.Adam([gain,bias],lr=1e-3)
if strategy: strategy.check_sanity(params, opts); state = strategy.initialize_state(scene_scale=scene_scale)
def ssim(x, y, ws=11):
    g = torch.exp(-((torch.arange(ws, device=x.device) - ws // 2) ** 2) / (2 * 1.5 ** 2)); g = g / g.sum()
    w = (g[:, None] * g[None, :])[None, None].repeat(3, 1, 1, 1)
    f = lambda t: Fn.conv2d(t, w, groups=3)
    mx, my = f(x), f(y); sxx = f(x * x) - mx ** 2; syy = f(y * y) - my ** 2; sxy = f(x * y) - mx * my
    C1, C2 = 0.01 ** 2, 0.03 ** 2
    return (((2 * mx * my + C1) * (2 * sxy + C2)) / ((mx ** 2 + my ** 2 + C1) * (sxx + syy + C2))).mean()
def render(i, mode='RGB'):
    sd = min(max(1, int(math.sqrt(1 + 0)) ) , a.sh)
    c = torch.cat([params['sh0'], params['shN']], 1)
    return rasterization(params['means'], Fn.normalize(params['quats'], dim=-1), torch.exp(params['scales']),
                         torch.sigmoid(params['opacities']), c, w2c[i:i + 1], Ks[i:i + 1], W, H, sh_degree=a.sh,
                         near_plane=0.03, far_plane=60.0, render_mode=mode, packed=False, absgrad=bool(a.absgrad))
torch.cuda.reset_peak_memory_stats(); torch.cuda.synchronize(); t0 = time.time()
order = []; log = []
for step in range(a.steps):
    if not order: order = list(np.random.permutation(train))
    i = order.pop()
    # progressive SH
    sh_deg = min(a.sh, step // 1000)
    mode = 'RGB+ED' if a.depth_w > 0 else 'RGB'
    c = torch.cat([params['sh0'], params['shN']], 1)
    rend, alpha, info = rasterization(params['means'], Fn.normalize(params['quats'], dim=-1), torch.exp(params['scales']),
                                      torch.sigmoid(params['opacities']), c, w2c[i:i + 1], Ks[i:i + 1], W, H, sh_degree=sh_deg,
                                      near_plane=0.03, far_plane=60.0, render_mode=mode, packed=False, absgrad=bool(a.absgrad))
    pred = rend[..., :3]; gt = img[i:i + 1]
    if a.expo: pred = pred * torch.exp(gain[i]) + bias[i]
    loss = (1 - a.ssim_w) * (pred - gt).abs().mean() + a.ssim_w * (1 - ssim(pred.permute(0, 3, 1, 2), gt.permute(0, 3, 1, 2)))
    if a.depth_w > 0:
        ed = rend[..., 3]; m = dep[i:i + 1] > 0
        if m.any(): loss = loss + a.depth_w * ((ed - dep[i:i + 1]).abs() / (dep[i:i + 1] + 0.3))[m].mean()
    if a.scale_reg > 0:
        sc_ = torch.exp(params['scales']); loss = loss + a.scale_reg * (sc_.max(1).values / sc_.min(1).values.clamp(min=1e-6)).clamp(min=4).sub(4).clamp(max=50).mean()
    if strategy: strategy.step_pre_backward(params, opts, state, step, info)
    loss.backward()
    if strategy: strategy.step_post_backward(params, opts, state, step, info, packed=False)
    for o in opts.values(): o.step(); o.zero_grad(set_to_none=True)
    if a.expo: eopt.step(); eopt.zero_grad(set_to_none=True)
    sched.step()
    if a.max_gs and len(params['means']) > a.max_gs: pass
    if step % 500 == 0: log.append((step, float(loss), len(params['means'])))
torch.cuda.synchronize(); t_train = time.time() - t0
peak = torch.cuda.max_memory_allocated() / 2 ** 20
# ---- eval
import lpips
lp = None if a.no_eval_lpips else lpips.LPIPS(net='alex', verbose=False).to(dev)
def evaluate(idx, tag):
    ps, ss, ls = [], [], []
    with torch.no_grad():
        for i in idx:
            rend, _, _ = render(i); pr = rend[..., :3].clamp(0, 1); gt = img[i:i + 1]
            mse = ((pr - gt) ** 2).mean(); ps.append(float(-10 * torch.log10(mse)))
            ss.append(float(ssim(pr.permute(0, 3, 1, 2), gt.permute(0, 3, 1, 2))))
            if lp is not None: ls.append(float(lp(pr.permute(0, 3, 1, 2) * 2 - 1, gt.permute(0, 3, 1, 2) * 2 - 1)))
    return {f'{tag}_psnr': float(np.mean(ps)), f'{tag}_ssim': float(np.mean(ss)), f'{tag}_lpips': float(np.mean(ls)) if ls else None}
res = dict(name=a.name, map=a.map, args=vars(a), n_train=len(train), n_test=len(test), img=[W, H], gaussians=len(params['means']),
           train_s=round(t_train, 1), it_per_s=round(a.steps / t_train, 1), load_s=round(t_load, 1), init_s=round(t_init, 2),
           init_points=int(Ng), raw_depth_points=int(raw_pts), peak_vram_mb=round(peak), scene_scale=scene_scale, log=log)
res.update(evaluate(test, 'test') if test else {}); res.update(evaluate(train[::4], 'train'))
# save renders
import torchvision.utils as vu
with torch.no_grad():
    rows = []
    for i in (test[:6] if test else train[::16][:6]):
        rend, _, _ = render(i, 'RGB+ED'); pr = rend[0, ..., :3].clamp(0, 1); ed = rend[0, ..., 3]
        edv = torch.from_numpy(cv2.applyColorMap((ed.clamp(0, 6) / 6 * 255).byte().cpu().numpy(), cv2.COLORMAP_TURBO)[..., ::-1].copy()).float().div(255).to(dev)
        rows.append(torch.cat([img[i], pr, edv], 1))
    vu.save_image(torch.cat(rows, 0).permute(2, 0, 1), f'{out}/heldout.png')
    # novel views: pose halfway between two train poses, shifted
    nv = []
    for (j, k) in ((train[10], train[11]), (train[40], train[41]), (train[70], train[71])):
        T = c2w[j].clone(); T[:3, 3] = 0.5 * (c2w[j][:3, 3] + c2w[k][:3, 3])
        ang = math.radians(25); R = torch.tensor([[math.cos(ang), 0, math.sin(ang)], [0, 1, 0], [-math.sin(ang), 0, math.cos(ang)]], device=dev)
        T[:3, :3] = T[:3, :3] @ R; T[:3, 3] += T[:3, :3][:, 0] * 0.3
        rend, _, _ = rasterization(params['means'], Fn.normalize(params['quats'], dim=-1), torch.exp(params['scales']), torch.sigmoid(params['opacities']),
                                   torch.cat([params['sh0'], params['shN']], 1), torch.linalg.inv(T)[None], Ks[j:j + 1], W, H, sh_degree=a.sh, near_plane=0.03)
        nv.append(rend[0, ..., :3].clamp(0, 1))
    vu.save_image(torch.cat(nv, 1).permute(2, 0, 1), f'{out}/novel.png')
# ---- export
from plyfile import PlyData, PlyElement
m = params['means'].detach().cpu().numpy(); n = len(m)
sh0 = params['sh0'].detach().cpu().numpy()[:, 0]; shN = params['shN'].detach().cpu().numpy().transpose(0, 2, 1).reshape(n, -1)
op = params['opacities'].detach().cpu().numpy(); sc = params['scales'].detach().cpu().numpy(); q = Fn.normalize(params['quats'], dim=-1).detach().cpu().numpy()
names = ['x', 'y', 'z', 'nx', 'ny', 'nz'] + [f'f_dc_{i}' for i in range(3)] + [f'f_rest_{i}' for i in range(shN.shape[1])] + ['opacity'] + [f'scale_{i}' for i in range(3)] + [f'rot_{i}' for i in range(4)]
arr = np.concatenate([m, np.zeros((n, 3)), sh0, shN, op[:, None], sc, q], 1).astype(np.float32)
el = np.empty(n, dtype=[(k, 'f4') for k in names])
for j, k in enumerate(names): el[k] = arr[:, j]
PlyData([PlyElement.describe(el, 'vertex')]).write(f'{out}/splat.ply')
# .splat (antimatter15): pos f32x3, scale f32x3 (linear), rgba u8x4, rot u8x4
rgb = np.clip((0.5 + SH0 * sh0) * 255, 0, 255).astype(np.uint8); alpha = (1 / (1 + np.exp(-op)) * 255).astype(np.uint8)
rot = np.clip(q * 128 + 128, 0, 255).astype(np.uint8)
idx = np.argsort(-(np.exp(sc).prod(1) / (1 + np.exp(-op))))
sp = np.concatenate([m[idx].astype('<f4').view(np.uint8).reshape(n, 12), np.exp(sc[idx]).astype('<f4').view(np.uint8).reshape(n, 12), rgb[idx], alpha[idx][:, None], rot[idx]], 1)
sp.tofile(f'{out}/splat.splat')
import gzip; open(f'{out}/splat.splat.gz', 'wb').write(gzip.compress(sp.tobytes(), 6))
# spz-like proxy: fixed point pos 24bit, scale u8 log, rot 3B, alpha u8, colour u8, + gzip
res['size_ply_mb'] = round(os.path.getsize(f'{out}/splat.ply') / 2 ** 20, 2)
res['size_splat_mb'] = round(os.path.getsize(f'{out}/splat.splat') / 2 ** 20, 2)
res['size_splat_gz_mb'] = round(os.path.getsize(f'{out}/splat.splat.gz') / 2 ** 20, 2)
json.dump(res, open(f'{out}/result.json', 'w'), indent=1)
print(json.dumps({k: v for k, v in res.items() if k not in ('args', 'log')}))
