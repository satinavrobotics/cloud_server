import json,cv2,numpy as np
from scipy.spatial import cKDTree
mp='office7'; F=json.load(open(f'data/{mp}/frames.json')); N=len(F)
C=np.array([f['c2w'] for f in F]); D=[cv2.imread(f'data/{mp}/depth/{f["id"]}.png',-1).astype(np.float32)/1000 for f in F]
K=np.array([[F[0]['fx'],0,F[0]['cx']],[0,F[0]['fy'],F[0]['cy']],[0,0,1]])
def pts(i):
    ys,xs=np.nonzero((D[i]>0.3)&(D[i]<6)); d=D[i][ys,xs]
    pc=np.stack([(xs-K[0,2])*d/K[0,0],(ys-K[1,2])*d/K[1,1],d],1); return pc@C[i,:3,:3].T+C[i,:3,3]
res=[];ang=[]
for i in range(3,N,8):
    # nearest frames with similar view dir
    best=[j for j in np.argsort(np.linalg.norm(C[:,:3,3]-C[i,:3,3],axis=1)) if j!=i and C[j,:3,2]@C[i,:3,2]>0.9][:3]
    w2c=np.linalg.inv(C[i])
    for j in best:
        P=pts(j); pc=P@w2c[:3,:3].T+w2c[:3,3]; z=pc[:,2]; ok=z>0.3
        u=(pc[:,0]/z*K[0,0]+K[0,2]).round().astype(int); v=(pc[:,1]/z*K[1,1]+K[1,2]).round().astype(int)
        ok&=(u>=0)&(u<448)&(v>=0)&(v<336); u,v,z=u[ok],v[ok],z[ok]; dg=D[i][v,u]; m=dg>0
        if m.sum()<200: continue
        r=np.abs(z[m]-dg[m])/dg[m]; res.append(np.median(r)); 
print('median relative depth residual across neighbours (test frame vs neighbour reprojection): median %.3f p90 %.3f n=%d'%(np.median(res),np.percentile(res,90),len(res)))
