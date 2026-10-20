"""Persistent chart keys. Rule A (the directive): a new chart takes the key of the previous chart it overlaps most; a key goes
to its largest claimant; losers are born with a new key; unclaimed previous keys are retired and never reused.
Needs only the previous owner plane (cell -> key) and the new one (cell -> chart index). O(cells)."""
import numpy as np

def overlaps(prev_key_plane, new_plane, nkeys_prev):
    """dict (new_chart j, prev key index k) -> number of cells in both"""
    m=(prev_key_plane>=0)&(new_plane>=0)
    if not m.any(): return {}
    code=new_plane[m].astype(np.int64)*(nkeys_prev+1)+prev_key_plane[m]
    u,c=np.unique(code,return_counts=True)
    return {(int(x//(nkeys_prev+1)),int(x%(nkeys_prev+1))):int(n) for x,n in zip(u,c)}

def overlaps_cells(prev_key_plane,ys,xs,labels,nkeys_prev):
    """`overlaps(prev_key_plane, new_plane, nkeys_prev)` for a new plane that is `labels[i]` at the cells (ys[i], xs[i]) and -1
    elsewhere: the same dict, in the same order, without touching the cells outside the new plane. The mask, the codes and their
    counts are those of `overlaps` restricted to the cells where new_plane >= 0 (the only ones its mask keeps); the counts of the
    sorted distinct codes come from a bincount when the code range is small, else from np.unique, as there."""
    pk=prev_key_plane[ys,xs]
    m=pk>=0
    if not m.any(): return {}
    nl=np.asarray(labels)[m].astype(np.int64)
    code=nl*(nkeys_prev+1)+pk[m]
    if len(code) and int(code.min())>=0 and int(code.max())<(1<<22):
        cnt=np.bincount(code)
        u=np.nonzero(cnt)[0]; c=cnt[u]
    else:
        u,c=np.unique(code,return_counts=True)
    return {(int(x//(nkeys_prev+1)),int(x%(nkeys_prev+1))):int(n) for x,n in zip(u,c)}

def assign_A(prev_keys,prev_plane,new_plane,anchors,birth,order,ov=None):
    """prev_keys: list of keys (tuples (birth_leaf, n)); prev_plane: cell -> index into prev_keys (or -1);
    new_plane: cell -> new chart index; anchors: list of (x,y) per new chart; returns keys per new chart, retired keys."""
    if ov is None: ov=overlaps(prev_plane,new_plane,len(prev_keys))
    nj=len(anchors); first=[None]*nj
    by_j={}
    for (j,k),n in ov.items(): by_j.setdefault(j,[]).append((-n,prev_keys[k],k))
    for j,l in by_j.items(): l.sort(); first[j]=(l[0][2],-l[0][0])           # largest overlap, ties: older key
    claim={}
    for j in range(nj):
        if first[j] is not None: claim.setdefault(first[j][0],[]).append(j)
    keys=[None]*nj; used=set()
    for k,js in claim.items():
        w=min(js,key=lambda j:(-first[j][1],anchors[j][1],anchors[j][0]))   # largest overlap, ties: anchor cell
        keys[w]=prev_keys[k]; used.add(k)
    n=0
    for j in range(nj):
        if keys[j] is None: keys[j]=(birth,n); n+=1
    retired=[prev_keys[k] for k in range(len(prev_keys)) if k not in used]
    return keys,retired
