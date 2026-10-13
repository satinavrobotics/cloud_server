import json,sys,numpy as np,cv2
from scipy.spatial import cKDTree
for mp in sys.argv[1:]:
    F=json.load(open(f'data/{mp}/frames.json'))
    C=np.array([f['c2w'] for f in F]); pos=C[:,:3,3]; fwd=C[:,:3,2]
    t=cKDTree(pos); d,idx=t.query(pos,k=2); nn=d[:,1]
    print(mp,'frames',len(F),'cams/node',len(F)/len({f['node'] for f in F}),'size',F[0]['w'],F[0]['h'],'rgb',F[0]['rgb_w'],F[0]['rgb_h'])
    print(' fx',F[0]['fx'],'hfov deg',np.degrees(2*np.arctan(F[0]['w']/2/F[0]['fx'])),'vfov',np.degrees(2*np.arctan(F[0]['h']/2/F[0]['fy'])),'dist',F[0]['dist'],'has_pose3d',np.mean([f['has_pose3d'] for f in F]))
    print(' NN dist m: min %.3f p10 %.3f med %.3f p90 %.3f max %.3f'%(nn.min(),*np.percentile(nn,[10,50,90]),nn.max()))
    # consecutive spacing
    cons=np.linalg.norm(np.diff(pos,axis=0),axis=1); print(' consecutive step m: med %.3f p90 %.3f max %.3f'%(np.median(cons),np.percentile(cons,90),cons.max()))
    # view dir difference to NN
    ang=np.degrees(np.arccos(np.clip((fwd*fwd[idx[:,1]]).sum(1),-1,1))); print(' view-dir angle to NN deg: med %.1f p90 %.1f max %.1f'%(np.median(ang),np.percentile(ang,90),ang.max()))
    # number of neighbours within 1m / 2m
    for r in (0.5,1,2): print(' mean #nodes within %.1fm: %.1f'%(r,np.mean([len(t.query_ball_point(p,r))-1 for p in pos])))
    # heading histogram (yaw of fwd in xy)
    yaw=np.degrees(np.arctan2(fwd[:,1],fwd[:,0])); h,_=np.histogram(yaw,bins=8,range=(-180,180)); print(' yaw hist 8 bins',h.tolist())
    print(' extent m',(pos.max(0)-pos.min(0)).round(1).tolist(),'cam height z range',pos[:,2].min().round(2),pos[:,2].max().round(2))
    # depth coverage
    cov=[];dm=[]
    for f in F[::5]:
        D=cv2.imread(f'data/{mp}/depth/{f["id"]}.png',-1); v=D>0; cov.append(v.mean()); dm.append(np.median(D[v])*0.001 if v.any() else 0)
    print(' depth valid frac mean %.2f; median depth %.2f m; depth dtype %s %s'%(np.mean(cov),np.mean(dm),D.dtype,D.shape))
    im=cv2.imread(f'data/{mp}/images/{F[0]["id"]}.jpg'); print(' rgb shape',im.shape)
