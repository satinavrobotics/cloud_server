import argparse, json, os, time, math
import numpy as np, cv2, torch, torch.nn.functional as Fn
p = argparse.ArgumentParser()
A = p.add_argument
A('--map', default='office7'); A('--name', required=True); A('--steps', type=int, default=2000)
A('--init', default='depth', choices=['depth', 'dense', 'cloud', 'random'])
A('--voxel', type=float, default=0.05); A('--init_n', type=int, default=0)       # subsample init to n (0 = all)
A('--strategy', default='none', choices=['none', 'default', 'mcmc']); A('--budget', type=int, default=200000)
A('--family', default='3dgs', choices=['3dgs', '2dgs']); A('--aa', type=int, default=0)
A('--sh', type=int, default=0); A('--split', default='every8', choices=['every8', 'block', 'all'])
A('--dd', default='', help='dense depth npy name: bil|inpaint|da2'); A('--dd_w', type=float, default=0.0)
A('--sparse_w', type=float, default=0.1)   # supervision on the raw lattice depth
A('--nrm_w', type=float, default=0.0); A('--tv_w', type=float, default=0.0)
A('--ssim_w', type=float, default=0.2); A('--expo', type=int, default=1)
A('--opa_reg', type=float, default=0.0); A('--scale_reg', type=float, default=0.0)
A('--smin', type=float, default=0.0); A('--smax', type=float, default=0.0)       # metres, clamp
A('--init_scale', type=float, default=1.0); A('--init_opa', type=float, default=0.3)
A('--lr_means', type=float, default=1.6e-4); A('--lr_scale', type=float, default=5e-3); A('--lr_opa', type=float, default=5e-2)
A('--curve', type=int, default=0); A('--mem_frac', type=float, default=0.3); A('--seed', type=int, default=0)
A('--poseopt', type=int, default=0); A('--pose_lr', type=float, default=1e-3); A('--pose_warm', type=int, default=300); A('--tt_steps', type=int, default=100)
A('--noise_lr', type=float, default=5e5); A('--refine_every', type=int, default=50)
a = p.parse_args()
torch.manual_seed(a.seed); np.random.seed(a.seed)
dev = 'cuda'; torch.cuda.set_per_process_memory_fraction(a.mem_frac)
from gsplat import rasterization
from gsplat.rendering import rasterization_2dgs
from gsplat.strategy import DefaultStrategy, MCMCStrategy
out = f'out/{a.name}'; os.makedirs(out, exist_ok=True)
F = json.load(open(f'data/{a.map}/frames.json')); N = len(F)
test = set(range(3, N, 8)) if a.split == 'every8' else (set(range(N // 2, N // 2 + 12)) if a.split == 'block' else set())
train = [i for i in range(N) if i not in test]; test = sorted(test)
def load(i):
    f = F[i]; im = cv2.cvtColor(cv2.imread(f'data/{a.map}/images/{f["id"]}.jpg'), cv2.COLOR_BGR2RGB)
    D = cv2.imread(f'data/{a.map}/depth/{f["id"]}.png', -1).astype(np.float32) * f['depth_scale']
    K = np.array([[f['fx'], 0, f['cx']], [0, f['fy'], f['cy']], [0, 0, 1]], np.float32)
    lo, hi = f['valid_range']; D[(D < lo) | (D > hi)] = 0
    return im, D, K, np.array(f['c2w'], np.float32)
data = [load(i) for i in range(N)]; H, W = data[0][0].shape[:2]
img = torch.from_numpy(np.stack([d[0] for d in data])).float().div(255).to(dev)
dep = torch.from_numpy(np.stack([d[1] for d in data])).to(dev)
Ks = torch.from_numpy(np.stack([d[2] for d in data])).to(dev)
c2w = torch.from_numpy(np.stack([d[3] for d in data])).to(dev); w2c = torch.linalg.inv(c2w)
dd = torch.from_numpy(np.load(f'data/{a.map}/dense_{a.dd}.npy')).to(dev) if a.dd else None
cpos = c2w[:, :3, 3]; center = cpos[train].mean(0); scene_scale = float((cpos[train] - center).norm(dim=1).max()) * 1.1
t_init = time.time()
def backproject(D, i, stride=1):
    v = D > 0
    if stride > 1:
        m = torch.zeros_like(v); m[::stride, ::stride] = True; v = v & m
    ys, xs = torch.nonzero(v, as_tuple=True); d = D[ys, xs]; K = Ks[i]
    pc = torch.stack([(xs - K[0, 2]) * d / K[0, 0], (ys - K[1, 2]) * d / K[1, 1], d], 1)
    return pc @ c2w[i, :3, :3].T + c2w[i, :3, 3], img[i][ys, xs]
def voxelize(P, C, vox):
    key = torch.floor(P / vox).long(); key = key - key.min(0).values; ext = key.max(0).values + 1
    lin = (key[:, 0] * ext[1] + key[:, 1]) * ext[2] + key[:, 2]
    uq, inv = torch.unique(lin, return_inverse=True)
    cnt = torch.zeros(len(uq), device=dev).index_add_(0, inv, torch.ones_like(lin, dtype=torch.float))
    return (torch.zeros(len(uq), 3, device=dev).index_add_(0, inv, P) / cnt[:, None], torch.zeros(len(uq), 3, device=dev).index_add_(0, inv, C) / cnt[:, None])
if a.init == 'depth':
    P, C = zip(*[backproject(dep[i], i) for i in train]); pts, cols = voxelize(torch.cat(P), torch.cat(C), a.voxel)
elif a.init == 'dense':
    P, C = zip(*[backproject(dd[i], i, 3) for i in train]); pts, cols = voxelize(torch.cat(P), torch.cat(C), a.voxel)
elif a.init == 'cloud':
    raw = np.fromfile(f'data/{a.map}/cloud.ply', dtype=np.uint8); hdr = raw[:2000].tobytes().split(b'end_header\n')[0]
    n = int(hdr.split(b'element vertex ')[1].split(b'\n')[0]); body = raw[len(hdr) + 11:]
    rec = np.frombuffer(body.tobytes(), dtype=[('x', '<f4'), ('y', '<f4'), ('z', '<f4'), ('r', 'u1'), ('g', 'u1'), ('b', 'u1'), ('c', '<u2')], count=n)
    pts = torch.from_numpy(np.stack([rec['x'], rec['y'], rec['z']], 1).copy()).to(dev); cols = torch.from_numpy(np.stack([rec['r'], rec['g'], rec['b']], 1).astype(np.float32) / 255).to(dev)
else:
    lo = cpos[train].min(0).values - 2; hi = cpos[train].max(0).values + 2
    pts = lo + torch.rand(a.init_n or 100000, 3, device=dev) * (hi - lo); cols = torch.rand(len(pts), 3, device=dev)
n_full = len(pts)
lim = a.init_n if a.init_n else (min(n_full, int(a.budget * 0.6)) if a.strategy == 'mcmc' else n_full)
if len(pts) > lim: sel = torch.randperm(len(pts), device=dev)[:lim]; pts, cols = pts[sel], cols[sel]
from scipy.spatial import cKDTree
pn = pts.cpu().numpy(); dist = torch.from_numpy(cKDTree(pn).query(pn, k=4)[0][:, 1:].mean(1)).float().to(dev).clamp(1e-3, 1.0) * a.init_scale
Ng = len(pts); t_init = time.time() - t_init
SH0 = 0.28209479177387814
params = {'means': pts.clone(), 'scales': torch.log(dist)[:, None].repeat(1, 3), 'quats': Fn.normalize(torch.randn(Ng, 4, device=dev), dim=-1),
          'opacities': torch.logit(torch.full((Ng,), a.init_opa, device=dev)), 'sh0': ((cols - 0.5) / SH0)[:, None, :],
          'shN': torch.zeros(Ng, (a.sh + 1) ** 2 - 1, 3, device=dev)}
params = {k: torch.nn.Parameter(v) for k, v in params.items()}
lrs = {'means': a.lr_means * scene_scale, 'scales': a.lr_scale, 'quats': 1e-3, 'opacities': a.lr_opa, 'sh0': 2.5e-3, 'shN': 2.5e-3 / 20}
opts = {k: torch.optim.Adam([params[k]], lr=lrs[k], eps=1e-15) for k in params}
sched = torch.optim.lr_scheduler.ExponentialLR(opts['means'], gamma=0.01 ** (1.0 / a.steps))
gain = torch.nn.Parameter(torch.zeros(N, 3, device=dev)); bias = torch.nn.Parameter(torch.zeros(N, 3, device=dev)); eopt = torch.optim.Adam([gain, bias], lr=1e-3)
if a.strategy == 'default':
    strat = DefaultStrategy(verbose=False, refine_start_iter=300, refine_stop_iter=int(a.steps * 0.6), refine_every=100, reset_every=max(a.steps // 3, 1000),
                            key_for_gradient='gradient_2dgs' if a.family == '2dgs' else 'means2d'); strat.check_sanity(params, opts); st = strat.initialize_state(scene_scale=scene_scale)
elif a.strategy == 'mcmc':
    strat = MCMCStrategy(cap_max=a.budget, noise_lr=a.noise_lr, refine_start_iter=200, refine_stop_iter=int(a.steps * 0.9), refine_every=a.refine_every, min_opacity=0.005)
    strat.check_sanity(params, opts); st = strat.initialize_state()
else: strat = None
pw = torch.nn.Parameter(torch.zeros(N, 3, device=dev)); pt = torch.nn.Parameter(torch.zeros(N, 3, device=dev))
popt = torch.optim.Adam([pw, pt], lr=a.pose_lr)
def rodr(w):
    th = w.norm(dim=-1, keepdim=True) + 1e-9; k = w / th; z = torch.zeros_like(k[..., 0])
    Kx = torch.stack([torch.stack([z, -k[..., 2], k[..., 1]], -1), torch.stack([k[..., 2], z, -k[..., 0]], -1), torch.stack([-k[..., 1], k[..., 0], z], -1)], -2)
    th = th[..., None]; return torch.eye(3, device=dev) + torch.sin(th) * Kx + (1 - torch.cos(th)) * Kx @ Kx
def view_of(i):
    if not a.poseopt: return w2c[i:i + 1]
    R = c2w[i, :3, :3] @ rodr(pw[i]); t = c2w[i, :3, 3] + c2w[i, :3, :3] @ pt[i]
    c = torch.cat([torch.cat([R, t[:, None]], 1), torch.tensor([[0, 0, 0, 1.]], device=dev)], 0); return torch.linalg.inv(c)[None]
def ssim(x, y, ws=11):
    g = torch.exp(-((torch.arange(ws, device=x.device) - ws // 2) ** 2) / (2 * 1.5 ** 2)); g = g / g.sum()
    w = (g[:, None] * g[None, :])[None, None].repeat(3, 1, 1, 1); f = lambda t: Fn.conv2d(t, w, groups=3)
    mx, my = f(x), f(y); sxx = f(x * x) - mx ** 2; syy = f(y * y) - my ** 2; sxy = f(x * y) - mx * my
    C1, C2 = 0.01 ** 2, 0.03 ** 2
    return (((2 * mx * my + C1) * (2 * sxy + C2)) / ((mx ** 2 + my ** 2 + C1) * (sxx + syy + C2))).mean()
def render(vm, K, mode='RGB', sh_deg=None, hw=None):
    h, w = hw or (H, W)
    c = torch.cat([params['sh0'], params['shN']], 1); q = Fn.normalize(params['quats'], dim=-1); s = torch.exp(params['scales']); o = torch.sigmoid(params['opacities'])
    if a.family == '2dgs':
        rc, ra, rn, nd, dist, med, info = rasterization_2dgs(params['means'], q, s, o, c, vm, K, w, h, sh_degree=a.sh if sh_deg is None else sh_deg, near_plane=0.03, render_mode=mode, packed=False)
        return rc, ra, info, rn
    kw = dict(rasterize_mode='antialiased') if a.aa else {}
    rc, ra, info = rasterization(params['means'], q, s, o, c, vm, K, w, h, sh_degree=a.sh if sh_deg is None else sh_deg, near_plane=0.03, far_plane=60.0, render_mode=mode, packed=False, **kw)
    return rc, ra, info, None
def dnormals(d, K):  # d: H,W metres -> normals (3,H,W) in camera frame, from finite differences of back-projected points
    ys, xs = torch.meshgrid(torch.arange(d.shape[0], device=dev), torch.arange(d.shape[1], device=dev), indexing='ij')
    P = torch.stack([(xs - K[0, 2]) * d / K[0, 0], (ys - K[1, 2]) * d / K[1, 1], d], 0)
    dx = P[:, 1:-1, 2:] - P[:, 1:-1, :-2]; dy = P[:, 2:, 1:-1] - P[:, :-2, 1:-1]
    return Fn.normalize(torch.cross(dx, dy, dim=0), dim=0)
def evaluate(idx, lp, tag):
    ps, ss, ls, pa, ph = [], [], [], [], []
    with torch.no_grad():
        for i in idx:
            rc, _, _, _ = render(view_of(i), Ks[i:i + 1]); pr = rc[..., :3].clamp(0, 1); gt = img[i:i + 1]
            ps.append(float(-10 * torch.log10(((pr - gt) ** 2).mean()))); ss.append(float(ssim(pr.permute(0, 3, 1, 2), gt.permute(0, 3, 1, 2))))
            if lp is not None: ls.append(float(lp(pr.permute(0, 3, 1, 2) * 2 - 1, gt.permute(0, 3, 1, 2) * 2 - 1)))
            # exposure-aligned PSNR (per-channel affine fit to GT; gives the upper bound if exposure were known)
            x = pr.reshape(-1, 3); y = gt.reshape(-1, 3); xm, ym = x.mean(0), y.mean(0); g_ = ((x - xm) * (y - ym)).sum(0) / ((x - xm) ** 2).sum(0).clamp(min=1e-8)
            pa.append(float(-10 * torch.log10((((x - xm) * g_ + ym - y) ** 2).mean())))
            ph.append(float(-10 * torch.log10(((Fn.avg_pool2d(pr.permute(0, 3, 1, 2), 2) - Fn.avg_pool2d(gt.permute(0, 3, 1, 2), 2)) ** 2).mean())))
    return {f'{tag}_psnr': float(np.mean(ps)), f'{tag}_ssim': float(np.mean(ss)), f'{tag}_lpips': float(np.mean(ls)) if ls else None, f'{tag}_psnr_aligned': float(np.mean(pa)), f'{tag}_psnr_half': float(np.mean(ph))}
torch.cuda.reset_peak_memory_stats(); torch.cuda.synchronize(); t0 = time.time(); order = []; curve = []
for step in range(a.steps):
    if not order: order = list(np.random.permutation(train))
    i = order.pop(); sh_deg = min(a.sh, step // 500)
    need_d = (a.dd_w > 0 or a.sparse_w > 0 or a.nrm_w > 0 or a.tv_w > 0)
    rc, ra, info, rn = render(view_of(i) if (a.poseopt and step >= a.pose_warm) else w2c[i:i + 1], Ks[i:i + 1], 'RGB+ED' if need_d else 'RGB', sh_deg)
    pred = rc[..., :3]; gt = img[i:i + 1]
    if a.expo: pred = pred * torch.exp(gain[i]) + bias[i]
    loss = (1 - a.ssim_w) * (pred - gt).abs().mean() + a.ssim_w * (1 - ssim(pred.permute(0, 3, 1, 2), gt.permute(0, 3, 1, 2)))
    if need_d:
        ed = rc[..., 3]
        if a.sparse_w > 0:
            m = dep[i:i + 1] > 0
            if m.any(): loss = loss + a.sparse_w * ((ed - dep[i:i + 1]).abs() / (dep[i:i + 1] + 0.3))[m].mean()
        if a.dd_w > 0: loss = loss + a.dd_w * ((ed - dd[i:i + 1]).abs() / (dd[i:i + 1] + 0.3)).mean()
        if a.nrm_w > 0:
            gd = dd[i] if dd is not None else dep[i]
            ng = dnormals(gd, Ks[i]); npd = dnormals(ed[0], Ks[i]) if rn is None else rn[0].permute(2, 0, 1)[:, 1:-1, 1:-1]
            mk = (gd[1:-1, 1:-1] > 0).float(); loss = loss + a.nrm_w * ((1 - (ng * npd).sum(0).abs()) * mk).mean()
        if a.tv_w > 0: loss = loss + a.tv_w * ((ed[:, 1:] - ed[:, :-1]).abs().mean() + (ed[:, :, 1:] - ed[:, :, :-1]).abs().mean())
    if a.opa_reg > 0: loss = loss + a.opa_reg * torch.sigmoid(params['opacities']).mean()
    if a.scale_reg > 0: loss = loss + a.scale_reg * torch.exp(params['scales']).mean()
    if strat and a.strategy == 'default': strat.step_pre_backward(params, opts, st, step, info)
    loss.backward()
    if strat and a.strategy == 'default': strat.step_post_backward(params, opts, st, step, info, packed=False)
    for o_ in opts.values(): o_.step(); o_.zero_grad(set_to_none=True)
    if a.expo: eopt.step(); eopt.zero_grad(set_to_none=True)
    if a.poseopt and step >= a.pose_warm: popt.step()
    popt.zero_grad(set_to_none=True)
    if a.smin or a.smax:
        with torch.no_grad(): params['scales'].clamp_(min=math.log(a.smin) if a.smin else -20, max=math.log(a.smax) if a.smax else 5)
    if strat and a.strategy == 'mcmc': strat.step_post_backward(params, opts, st, step, info, lr=sched.get_last_lr()[0])
    sched.step()
    if a.curve and test and (step + 1) % a.curve == 0:
        torch.cuda.synchronize(); curve.append((step + 1, round(evaluate(test, None, 't')['t_psnr'], 2), len(params['means'])))
torch.cuda.synchronize(); t_train = time.time() - t0 - 0  # curve evals included only if --curve
peak = torch.cuda.max_memory_allocated() / 2 ** 20
res_pose = {}
if a.poseopt and test:
    for k_, v_ in evaluate(test, None, 'rawpose').items(): res_pose[k_] = v_
    tp = [torch.nn.Parameter(torch.zeros(3, device=dev)) for _ in test]; tq = [torch.nn.Parameter(torch.zeros(3, device=dev)) for _ in test]
    for pp in params.values(): pp.requires_grad_(False)
    for k_, i in enumerate(test):
        o2 = torch.optim.Adam([tp[k_], tq[k_]], lr=2e-3)
        for _ in range(a.tt_steps):
            pw.data[i] = tp[k_].data; pt.data[i] = tq[k_].data
            R = c2w[i, :3, :3] @ rodr(tp[k_]); t = c2w[i, :3, 3] + c2w[i, :3, :3] @ tq[k_]
            vm = torch.linalg.inv(torch.cat([torch.cat([R, t[:, None]], 1), torch.tensor([[0, 0, 0, 1.]], device=dev)], 0))[None]
            rc, _, _, _ = render(vm, Ks[i:i + 1]); l = (rc[..., :3] - img[i:i + 1]).abs().mean(); o2.zero_grad(); l.backward(); o2.step()
        pw.data[i] = tp[k_].data; pt.data[i] = tq[k_].data
    res_pose.update(dict(train_rot_deg=float(pw[train].norm(dim=1).mean()) * 57.3, train_trans_cm=float(pt[train].norm(dim=1).mean()) * 100,
                    test_rot_deg=float(pw[test].norm(dim=1).mean()) * 57.3, test_trans_cm=float(pt[test].norm(dim=1).mean()) * 100))
import lpips; lp = lpips.LPIPS(net='alex', verbose=False).to(dev)
res = dict(name=a.name, args=vars(a), n_train=len(train), n_test=len(test), gaussians=len(params['means']), train_s=round(t_train, 1), init_s=round(t_init, 2), peak_vram_mb=round(peak),
           init_points=Ng, curve=curve)
res.update(res_pose); res.update(evaluate(test, lp, 'test') if test else {}); res.update(evaluate(train[::4], lp, 'train'))
import torchvision.utils as vu
HO = [3, 27, 43, 59, 75, 83] if a.split == 'every8' else test[::2][:6]
HO = [i for i in HO if i in test] or test[:6]
with torch.no_grad():
    rows = []
    for i in HO:
        rc, _, _, _ = render(view_of(i), Ks[i:i + 1], 'RGB+ED'); pr = rc[0, ..., :3].clamp(0, 1); ed = rc[0, ..., 3]
        edv = torch.from_numpy(cv2.applyColorMap((ed.clamp(0, 6) / 6 * 255).byte().cpu().numpy(), cv2.COLORMAP_TURBO)[..., ::-1].copy()).float().div(255).to(dev)
        rows.append(torch.stack([pr, edv]))   # 2,H,W,3
    torch.save(torch.stack(rows).cpu(), f'{out}/heldout_renders.pt')
    nv = []; NVP = []
    for (j, k) in ((train[10], train[11]), (train[40], train[41]), (train[70], train[71])):
        T = c2w[j].clone(); T[:3, 3] = 0.5 * (c2w[j][:3, 3] + c2w[k][:3, 3]); ang = math.radians(25)
        R = torch.tensor([[math.cos(ang), 0, math.sin(ang)], [0, 1, 0], [-math.sin(ang), 0, math.cos(ang)]], device=dev)
        T[:3, :3] = T[:3, :3] @ R; T[:3, 3] += T[:3, :3][:, 0] * 0.3
        rc, _, _, _ = render(torch.linalg.inv(T)[None], Ks[j:j + 1], 'RGB+ED'); ed = rc[0, ..., 3]
        edv = torch.from_numpy(cv2.applyColorMap((ed.clamp(0, 6) / 6 * 255).byte().cpu().numpy(), cv2.COLORMAP_TURBO)[..., ::-1].copy()).float().div(255).to(dev)
        nv.append(torch.stack([rc[0, ..., :3].clamp(0, 1), edv]))
    torch.save(torch.stack(nv).cpu(), f'{out}/novel_renders.pt')
m = params['means'].detach().cpu().numpy(); n = len(m); sc = params['scales'].detach().cpu().numpy(); op = params['opacities'].detach().cpu().numpy()
q = Fn.normalize(params['quats'], dim=-1).detach().cpu().numpy(); sh0 = params['sh0'].detach().cpu().numpy()[:, 0]
rgb = np.clip((0.5 + SH0 * sh0) * 255, 0, 255).astype(np.uint8); alpha = (1 / (1 + np.exp(-op)) * 255).astype(np.uint8); rot = np.clip(q * 128 + 128, 0, 255).astype(np.uint8)
sp = np.concatenate([m.astype('<f4').view(np.uint8).reshape(n, 12), np.exp(sc).astype('<f4').view(np.uint8).reshape(n, 12), rgb, alpha[:, None], rot], 1); sp.tofile(f'{out}/splat.splat')
res['size_splat_mb'] = round(os.path.getsize(f'{out}/splat.splat') / 2 ** 20, 2); res['size_ply_mb_est'] = round(n * (14 + 3 * (a.sh + 1) ** 2 - 3 + 0) * 4 / 2 ** 20, 1)
json.dump(res, open(f'{out}/result.json', 'w'), indent=1)
print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in res.items() if k not in ('args',)}))
