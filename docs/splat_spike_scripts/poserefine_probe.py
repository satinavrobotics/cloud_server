import json,cv2,numpy as np,torch,torch.nn.functional as Fn,math
exec(open('scripts/warpcheck.py').read().split("res=[];res_s=[]")[0])
def rodrigues(w):
    th=w.norm()+1e-9; k=w/th; z0=torch.zeros((),device=dev); Kx=torch.stack([torch.stack([z0,-k[2],k[1]]),torch.stack([k[2],z0,-k[0]]),torch.stack([-k[1],k[0],z0])])
    return torch.eye(3,device=dev)+torch.sin(th)*Kx+(1-torch.cos(th))*Kx@Kx
out=[]
for i in test[:12]:
    d=dd[i]; P=torch.stack([(xs-K[0,2])*d/K[0,0],(ys-K[1,2])*d/K[1,1],d],-1).reshape(-1,3)@C[i,:3,:3].T+C[i,:3,3]
    j=sorted(train,key=lambda j: float((C[j,:3,3]-C[i,:3,3]).norm())+0.3*(1-float(C[j,:3,2]@C[i,:3,2])))[0]
    def cost(dw,dt):
        Rj=rodrigues(dw)@C[j,:3,:3]; Cj=torch.cat([torch.cat([Rj,(C[j,:3,3]+dt)[:,None]],1),torch.tensor([[0,0,0,1.]],device=dev)],0)
        w=torch.linalg.inv(Cj); pc=P@w[:3,:3].T+w[:3,3]; z=pc[:,2].clamp(min=0.05); u=pc[:,0]/z*K[0,0]+K[0,2]; v=pc[:,1]/z*K[1,1]+K[1,2]
        ok=((pc[:,2]>0.2)&(u>=1)&(u<446)&(v>=1)&(v<334)).reshape(336,448)
        g=torch.stack([u/447*2-1,v/335*2-1],-1).reshape(1,336,448,2); s=Fn.grid_sample(img[j:j+1],g,align_corners=True,padding_mode='border')[0]
        e=((s-img[i])**2).mean(0)[ok]; return e.mean(),ok.float().mean()
    dw=torch.zeros(3,device=dev,requires_grad=True); dt=torch.zeros(3,device=dev,requires_grad=True); opt=torch.optim.Adam([dw,dt],lr=2e-3)
    c0=float(cost(dw,dt)[0].detach())
    for _ in range(150): l,_=cost(dw,dt); opt.zero_grad(); l.backward(); opt.step()
    c1=float(cost(dw,dt)[0].detach()); out.append((-10*math.log10(c0),-10*math.log10(c1),float(dw.norm())*57.3,float(dt.norm())*100))
o=np.array(out); print('PSNR before %.2f after-6DoF-fit %.2f ; |dRot| %.2f deg ; |dTrans| %.1f cm (means over %d pairs)'%(*o.mean(0)[:2],*o.mean(0)[2:],len(o))); print(o.round(2))
