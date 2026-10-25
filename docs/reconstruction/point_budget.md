# Bounded cloud: `cloud.ply?max_points=N`

Code: `packages/api/reconstruction_lod.py`, route `get_map_reconstruction_file` in
`packages/api/main.py`. Tests: `tests/unit/test_map_reconstruction_lod.py`.

## Contract

`GET /api/v1/maps/{map}/reconstruction/files/cloud.ply?max_points=N`, N in 1000..20 000 000
(422 otherwise; ignored for the other files; without it nothing changes).

* N is rounded **up** to a bucket: 1k 2k 5k 10k 25k 50k 100k 250k 500k 1M 2M 3M 5M 10M 20M.
  The answer never has more than that bucket (so at most 2.5 x N, exactly N when N is a bucket).
* A binary_little_endian vertex-only PLY with more vertices than the bucket is answered with every
  k-th vertex, k = ceil(total / bucket); returned = ceil(total / k) <= bucket (often a bit below,
  e.g. 4 100 000 points at 3M gives k = 2, 2 050 000 returned). Properties are kept, `element vertex`
  is rewritten, a `comment satinav lod stride=k total=T` line is added.
* `X-Total-Points`, `X-Returned-Points` on every such answer (also when the file is small enough and
  is sent whole: both = total). Missing = the file is not a PLY we decimate (ASCII, big endian,
  other elements, size not matching the header, truncated): the full file is sent, count unknown.
* `Content-Length` is exact, `ETag` is `"<job id>-lod<bucket>"` (a 304 needs no work and is answered
  before anything is read), `Content-Disposition` and the cache rules are as for the full file.
* `GET .../reconstruction`: `reconstruction.files.cloud.points` is the vertex count (from the job
  result) so a client can choose N before downloading.

## How

The first request of a (file, bucket) reads the source object once, sequentially (4 MiB chunks),
keeps every k-th record and writes a temp file that is renamed (atomic). No bytes go to the client
before it is done (the headers need the final size). Later requests are a plain `FileResponse`.
Concurrent requests for one entry wait for the first. The cache key is
sha1(bucket, key, size) + bucket: the key contains the job id, so a rebuilt cloud never hits an
old entry. Cache: `RECONSTRUCTION_LOD_CACHE_DIR` (default `<tmp>/satinav-recon-lod`),
`..._MAX_FILES` (6), `..._MAX_MB` (1536); least recently used entries go first, the one just
written never; stale `.tmp-*` files (> 1 h) too.

Measured (dev box, local file, page cache warm, not the Pi): 60 M points = 1.02 GB source,
bucket 3M: 0.9 s, 51 MB out; bucket 250k: 0.2 s; extra resident memory about 16 MB.

## nginx

`/api/` had `gzip on` for application/octet-stream and default proxy buffering, so cloud.ply was
gzipped on the fly (CPU on a Pi, no Content-Length) and spooled; `proxy_read_timeout` was 60 s,
shorter than a first build of a big cloud could be. `routes.conf` now has a regex location for
`/reconstruction/files/<name>`: `gzip off`, `proxy_buffering off`, 600 s timeouts, and
`Access-Control-Expose-Headers` for the two counters (a browser cannot read them otherwise).
Not yet exercised against a running nginx.
