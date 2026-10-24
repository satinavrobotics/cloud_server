import json,sys,time,numpy as np,cv2,torch
from transformers import pipeline
mp=sys.argv[1]; F=json.load(open(f'data/{mp}/frames.json'))
pipe=pipeline('depth-estimation',model='depth-anything/Depth-Anything-V2-Small-hf',device=0)
out=np.zeros((len(F),336,448),np.float32); out_inp=np.zeros_like(out); stats=[]
t=time.time()
for k,f in enumerate(F):
    im=cv2.cvtColor(cv2.imread(f'data/{mp}/images/{f["id"]}.jpg'),cv2.COLOR_BGR2RGB)
    from PIL import Image
    r=pipe(Image.fromarray(im))['predicted_depth'][0].cpu().numpy()   # relative inverse depth (disparity)
    r=cv2.resize(r,(448,336),interpolation=cv2.INTER_LINEAR)
    D=cv2.imread(f'data/{mp}/depth/{f["id"]}.png',-1).astype(np.float32)*f['depth_scale']
    lo,hi=f['valid_range']; v=(D>lo)&(D<hi)
    ys,xs=np.nonzero(v); idx=np.arange(len(ys)); rng=np.random.RandomState(k); rng.shuffle(idx); nh=len(idx)//5
    hold,fit=idx[:nh],idx[nh:]
    def solve(sel):  # fit inverse depth = a*disp+b (robust, 3 iterations of trimmed lsq)
        x=r[ys[sel],xs[sel]];y=1/D[ys[sel],xs[sel]];w=np.ones(len(x))
        for _ in range(4):
            A=np.stack([x,np.ones_like(x)],1)*w[:,None]; a,b=np.linalg.lstsq(A,y*w,rcond=None)[0]
            res=np.abs(a*x+b-y); w=(res<np.percentile(res,80)).astype(float)
        return a,b
    a,b=solve(fit); inv=np.clip(a*r+b,1/20,1/0.15); dd=1/inv
    e=np.abs(dd[ys[hold],xs[hold]]-D[ys[hold],xs[hold]])/D[ys[hold],xs[hold]]
    # lattice inpaint baseline
    Dm=np.where(v,D,0).astype(np.float32); m=(~v).astype(np.uint8)
    inp=cv2.inpaint(Dm,m,3,cv2.INPAINT_TELEA)
    e2=[]  # holdout for inpaint
    stats.append((float(np.median(e)),float(np.mean(e)),float(np.median(np.abs(inp[ys[hold],xs[hold]]-D[ys[hold],xs[hold]])/D[ys[hold],xs[hold]]))))
    # final dense: refit with all points, keep measured depth where valid
    a,b=solve(np.arange(len(ys))); dd=1/np.clip(a*r+b,1/20,1/0.15); dd=np.where(v,D,dd)
    out[k]=dd; out_inp[k]=cv2.inpaint(Dm,m,3,cv2.INPAINT_TELEA)
torch.cuda.synchronize(); el=time.time()-t
s=np.array(stats); print('DAv2 aligned: held-out lattice rel err median %.3f mean %.3f ; telea inpaint median %.3f ; %.1fs for %d frames'%(np.median(s[:,0]),s[:,1].mean(),np.median(s[:,2]),el,len(F)))
np.save(f'data/{mp}/dense_da2.npy',out); np.save(f'data/{mp}/dense_inpaint.npy',out_inp)
