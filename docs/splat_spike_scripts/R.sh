n=$1; shift; venv/bin/python scripts/train.py --name $n "$@" 2>&1 | grep '^{' | python3 -c "
import sys,json
for l in sys.stdin:
    r=json.loads(l);print(r['name'],'gs',r['gaussians'],'t',r['train_s'],'vram',r['peak_vram_mb'],'PSNR %.2f SSIM %.3f LPIPS %.3f'%(r['test_psnr'],r['test_ssim'],r['test_lpips']),'trainPSNR %.2f'%r['train_psnr'],'ply',r['size_ply_mb'],'splat',r['size_splat_mb'],flush=True)"
