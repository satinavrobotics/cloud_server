n=$1; shift; venv/bin/python scripts/train2.py --name $n "$@" 2>&1 | grep -E '^\{|Error' | python3 -c "
import sys,json
for l in sys.stdin:
    if not l.startswith('{'): print('  ',l.strip()[:200]); continue
    r=json.loads(l);print('%-24s gs %7d t %5.1fs vram %4d | PSNR %.2f (raw-pose %s) al %.2f SSIM %.3f LPIPS %.3f | train %.2f | %.1fMB'%(r['name'],r['gaussians'],r['train_s'],r['peak_vram_mb'],r['test_psnr'],('%.2f'%r['rawpose_psnr']) if 'rawpose_psnr' in r else '-',r['test_psnr_aligned'],r['test_ssim'],r['test_lpips'],r['train_psnr'],r['size_splat_mb']),flush=True)"
