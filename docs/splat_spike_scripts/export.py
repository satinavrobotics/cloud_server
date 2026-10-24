import os,sys,json,io,numpy as np
from concurrent.futures import ThreadPoolExecutor
from arango import ArangoClient
from minio import Minio
mp=sys.argv[1]
out=f"data/{mp}"; os.makedirs(out+"/images",exist_ok=True); os.makedirs(out+"/depth",exist_ok=True)
c=ArangoClient(hosts=f"http://{os.environ['ARANGO_HOST']}:{os.environ['ARANGO_PORT']}")
db=c.db('topomap_db',username=os.environ['ARANGO_USERNAME'],password=os.environ['ARANGO_PASSWORD'])
docs=list(db.aql.execute("FOR d IN @@c FILTER d.depth!=null RETURN {node_id:d.node_id,pose:d.pose,depth:d.depth,created_at:d.created_at}",bind_vars={'@c':'nodes_'+mp}))
docs.sort(key=lambda d:(d['created_at'],d['node_id']))
m=Minio("localhost:9000",os.environ['MINIO_ACCESS_KEY'],os.environ['MINIO_SECRET_KEY'],secure=False)
b='map-'+mp
def q2R(qx,qy,qz,qw):
    n=np.sqrt(qx*qx+qy*qy+qz*qz+qw*qw);qx,qy,qz,qw=qx/n,qy/n,qz/n,qw/n
    return np.array([[1-2*(qy**2+qz**2),2*(qx*qy-qz*qw),2*(qx*qz+qy*qw)],[2*(qx*qy+qz*qw),1-2*(qx**2+qz**2),2*(qy*qz-qx*qw)],[2*(qx*qz-qy*qw),2*(qy*qz+qx*qw),1-2*(qx**2+qy**2)]])
frames=[]
jobs=[]
for d in docs:
    for cam,rec in d['depth'].items():
        if not isinstance(rec.get('camera'),dict): continue
        p=rec['camera']; tb=p['T_base_cam']
        Rbc=q2R(tb['qx'],tb['qy'],tb['qz'],tb['qw']); tbc=np.array([tb['x'],tb['y'],tb['z']])
        pm=rec.get('pose3d_map') or rec.get('robot_pose3d')
        if pm: Rmb=q2R(pm['qx'],pm['qy'],pm['qz'],pm['qw']); tmb=np.array([pm['x'],pm['y'],pm['z']])
        else:
            y=d['pose']['yaw']; Rmb=np.array([[np.cos(y),-np.sin(y),0],[np.sin(y),np.cos(y),0],[0,0,1]]); tmb=np.array([d['pose']['x'],d['pose']['y'],0])
        c2w=np.eye(4); c2w[:3,:3]=Rmb@Rbc; c2w[:3,3]=Rmb@tbc+tmb
        fn=f"{d['node_id']}_{cam}"
        frames.append(dict(id=fn,node=d['node_id'],cam=cam,w=p['width'],h=p['height'],rgb_w=p['rgb_width'],rgb_h=p['rgb_height'],fx=p['fx'],fy=p['fy'],cx=p['cx'],cy=p['cy'],dist=p['d'],model=p['distortion_model'],depth_scale=rec.get('depth_scale',0.001),valid_range=p['valid_range_m'],has_pose3d=bool(pm),c2w=c2w.tolist()))
        jobs.append((fn,d['node_id'],cam))
def dl(j):
    fn,n,cam=j
    for key,dst in ((f"{n}/images/{cam}",f"{out}/images/{fn}.jpg"),(f"{n}/depth/{cam}.png",f"{out}/depth/{fn}.png")):
        if os.path.exists(dst): continue
        try:
            r=m.get_object(b,key); open(dst,'wb').write(r.read()); r.close(); r.release_conn()
        except Exception as e: print('ERR',key,str(e)[:80])
with ThreadPoolExecutor(8) as ex: list(ex.map(dl,jobs))
json.dump(frames,open(out+"/frames.json","w"))
print(mp,len(frames),'frames')
