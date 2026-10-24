import json,sys,time,numpy as np,cv2,torch,torch.nn.functional as Fn
mp=sys.argv[1]; F=json.load(open(f'data/{mp}/frames.json')); dev='cuda'
def densify(D,I,mask,R=5,ss=2.0,sr=0.08,iters=2):
    # D,mask: H,W ; I: 3,H,W in [0,1]; joint bilateral normalized convolution; repeated to fill
    H,W=D.shape; Dm=D*mask; M=mask.clone().float()
    out=Dm.clone(); known=mask.clone().float()
    ys,xs=torch.meshgrid(torch.arange(-R,R+1,device=dev),torch.arange(-R,R+1,device=dev),indexing='ij')
    ws=torch.exp(-(ys**2+xs**2).float()/(2*ss**2)).reshape(1,-1,1)
    Ip=Fn.unfold(Fn.pad(I[None],(R,R,R,R),mode='replicate'),2*R+1)            # 1,3*K,HW
    K=(2*R+1)**2; Ip=Ip.reshape(3,K,H*W); Ic=I.reshape(3,1,H*W)
    wr=torch.exp(-((Ip-Ic)**2).sum(0)/(2*sr**2))[None]                         # 1,K,HW
    for _ in range(iters):
        Dp=Fn.unfold(Fn.pad(out[None,None],(R,R,R,R)),2*R+1); Kp=Fn.unfold(Fn.pad(known[None,None],(R,R,R,R)),2*R+1)
        w=ws*wr*Kp; num=(w*Dp).sum(1); den=w.sum(1)
        new=(num/den.clamp(min=1e-8)).reshape(H,W); ok=(den.reshape(H,W)>1e-3)
        fill=(known==0)&ok; out=torch.where(fill,new,out); known=torch.where(fill,torch.ones_like(known),known)
    return out,known
if __name__=='__main__':
    errs=[];t=time.time();dense=np.zeros((len(F),336,448),np.float32);kn=[]
    for k,f in enumerate(F):
        I=torch.from_numpy(cv2.cvtColor(cv2.imread(f'data/{mp}/images/{f["id"]}.jpg'),cv2.COLOR_BGR2RGB)).float().div(255).permute(2,0,1).to(dev)
        D=torch.from_numpy(cv2.imread(f'data/{mp}/depth/{f["id"]}.png',-1).astype(np.float32)*f['depth_scale']).to(dev)
        lo,hi=f['valid_range']; v=(D>lo)&(D<hi)
        g=torch.Generator(device=dev).manual_seed(k); hold=v&(torch.rand(D.shape,device=dev,generator=g)<0.2)
        o,kk=densify(D,I,v&~hold)
        e=((o-D).abs()/D)[hold&(kk>0)]; errs.append((float(e.median()),float((e<0.05).float().mean()),float((hold&(kk>0)).sum()/hold.sum())))
        o,kk=densify(D,I,v); dense[k]=torch.where(v,D,o).cpu().numpy(); kn.append(float(kk.mean()))
    torch.cuda.synchronize(); s=np.array(errs)
    print('bilateral fill: holdout(20%% lattice) rel err median %.3f, frac<5%% %.2f, covered %.2f; coverage of full frame %.2f; %.1fs'%(np.median(s[:,0]),s[:,1].mean(),s[:,2].mean(),np.mean(kn),time.time()-t))
    np.save(f'data/{mp}/dense_bil.npy',dense)
