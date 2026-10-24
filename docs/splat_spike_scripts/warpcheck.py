import json,cv2,numpy as np,torch,torch.nn.functional as Fn
mp='office7'; F=json.load(open(f'data/{mp}/frames.json')); N=len(F); dev='cuda'
C=torch.tensor([f['c2w'] for f in F],dtype=torch.float32,device=dev)
img=torch.from_numpy(np.stack([cv2.cvtColor(cv2.imread(f'data/{mp}/images/{f["id"]}.jpg'),cv2.COLOR_BGR2RGB) for f in F])).float().div(255).to(dev).permute(0,3,1,2)
dd=torch.from_numpy(np.load(f'data/{mp}/dense_bil.npy')).to(dev)
K=torch.tensor([[F[0]['fx'],0,F[0]['cx']],[0,F[0]['fy'],F[0]['cy']],[0,0,1.]],device=dev)
ys,xs=torch.meshgrid(torch.arange(336,device=dev),torch.arange(448,device=dev),indexing='ij')
test=list(range(3,N,8)); train=[i for i in range(N) if i not in test]
res=[];res_s=[]
for i in test:
    d=dd[i]; P=torch.stack([(xs-K[0,2])*d/K[0,0],(ys-K[1,2])*d/K[1,1],d],-1).reshape(-1,3)@C[i,:3,:3].T+C[i,:3,3]
    cand=sorted(train,key=lambda j: float((C[j,:3,3]-C[i,:3,3]).norm())+0.3*(1-float(C[j,:3,2]@C[i,:3,2])))[:4]
    for j in cand[:2]:
        w=torch.linalg.inv(C[j]); pc=P@w[:3,:3].T+w[:3,3]; z=pc[:,2]; u=pc[:,0]/z*K[0,0]+K[0,2]; v=pc[:,1]/z*K[1,1]+K[1,2]
        ok=(z>0.2)&(u>=1)&(u<446)&(v>=1)&(v<334)
        g=torch.stack([u/447*2-1,v/335*2-1],-1).reshape(1,336,448,2)
        samp=Fn.grid_sample(img[j:j+1],g,align_corners=True,padding_mode='border')[0]
        ok=ok.reshape(336,448); x=samp.permute(1,2,0)[ok]; y=img[i].permute(1,2,0)[ok]
        # exposure-aligned
        xm,ym=x.mean(0),y.mean(0); gg=((x-xm)*(y-ym)).sum(0)/((x-xm)**2).sum(0).clamp(min=1e-8)
        res.append((float(-10*torch.log10(((x-y)**2).mean())),float(-10*torch.log10((((x-xm)*gg+ym-y)**2).mean())),float(ok.float().mean())))
r=np.array(res); print(np.isnan(r).sum(), 'GT-neighbour warp (dense depth, pose from manifest): PSNR raw %.2f aligned %.2f overlap %.2f (n=%d)'%(np.nanmean(r[:,0]),np.nanmean(r[:,1]),np.nanmean(r[:,2]),len(r)), np.nanmedian(r[:,0]))
