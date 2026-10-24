import json,cv2,numpy as np,torch,torch.nn.functional as Fn,math,sys
exec(open('scripts/warpcheck.py').read().split("res=[];res_s=[]")[0])
exec(open('scripts/poserefine_probe.py').read().split("out=[]")[0].split("def rodrigues")[1].join(["def rodrigues",""]) ) if False else None
def rodrigues(w):
    th=w.norm()+1e-9; k=w/th; z0=torch.zeros((),device=dev); Kx=torch.stack([torch.stack([z0,-k[2],k[1]]),torch.stack([k[2],z0,-k[0]]),torch.stack([-k[1],k[0],z0])])
    return torch.eye(3,device=dev)+torch.sin(th)*Kx+(1-torch.cos(th))*Kx@Kx
def mk(dw,dt): return torch.cat([torch.cat([rodrigues(dw),dt[:,None]],1),torch.tensor([[0,0,0,1.]],device=dev)],0)
def nn(i,pool): return sorted([j for j in pool if j!=i],key=lambda j: float((C[j,:3,3]-C[i,:3,3]).norm())+0.3*(1-float(C[j,:3,2]@C[i,:3,2])))[0]
pairs_tr=[(i,nn(i,train)) for i in train[::2]]; pairs_te=[(i,nn(i,train)) for i in test]
def cost(Dl,pairs):
    es=[]
    for i,j in pairs:
        Ci=C[i]@Dl; Cj=C[j]@Dl; d=dd[i]
        P=torch.stack([(xs-K[0,2])*d/K[0,0],(ys-K[1,2])*d/K[1,1],d],-1).reshape(-1,3)@Ci[:3,:3].T+Ci[:3,3]
        w=torch.linalg.inv(Cj); pc=P@w[:3,:3].T+w[:3,3]; z=pc[:,2].clamp(min=0.05); u=pc[:,0]/z*K[0,0]+K[0,2]; v=pc[:,1]/z*K[1,1]+K[1,2]
        ok=((pc[:,2]>0.2)&(u>=1)&(u<446)&(v>=1)&(v<334)).reshape(336,448)
        g=torch.stack([u/447*2-1,v/335*2-1],-1).reshape(1,336,448,2); s=Fn.grid_sample(img[j:j+1],g,align_corners=True,padding_mode='border')[0]
        es.append(((s-img[i])**2).mean(0)[ok].mean())
    return torch.stack(es).mean()
dw=torch.zeros(3,device=dev,requires_grad=True); dt=torch.zeros(3,device=dev,requires_grad=True); opt=torch.optim.Adam([dw,dt],lr=2e-3)
psnr=lambda e:-10*math.log10(float(e))
print('before: train-pairs %.2f test-pairs %.2f'%(psnr(cost(mk(dw,dt),pairs_tr).detach()),psnr(cost(mk(dw,dt),pairs_te).detach())))
for it in range(120):
    sub=[pairs_tr[k] for k in np.random.choice(len(pairs_tr),12,replace=False)]
    l=cost(mk(dw,dt),sub); opt.zero_grad(); l.backward(); opt.step()
print('shared camera-frame correction: rot(deg)',(dw*57.3).detach().cpu().numpy().round(2),'trans(cm)',(dt*100).detach().cpu().numpy().round(1))
print('after : train-pairs %.2f test-pairs %.2f'%(psnr(cost(mk(dw,dt),pairs_tr).detach()),psnr(cost(mk(dw,dt),pairs_te).detach())))
