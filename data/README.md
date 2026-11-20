# Data

## GPS trajectories

PriVELA uses the [GeoLife GPS Trajectories](https://www.microsoft.com/en-us/download/details.aspx?id=52367)
dataset (Microsoft Research Asia). Download it into `data/raw/` and run `privela prepare-data` as
described in the main README; `data/raw/` and `data/processed/` are git-ignored. Please cite the
dataset:

1. Y. Zheng, L. Zhang, X. Xie, W.-Y. Ma. *Mining interesting locations and travel sequences
   from GPS trajectories.* WWW 2009.
2. Y. Zheng, Q. Li, Y. Chen, X. Xie, W.-Y. Ma. *Understanding mobility based on GPS data.*
   UbiComp 2008.
3. Y. Zheng, X. Xie, W.-Y. Ma. *GeoLife: A collaborative social networking service among user,
   location and trajectory.* IEEE Data Engineering Bulletin 33(2), 2010.

## `road/subgraph.json` — OpenStreetMap

A 45-node road subgraph of Beijing derived from [OpenStreetMap](https://www.openstreetmap.org)
data, © OpenStreetMap contributors, available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). It is used
by the road kernels until `privela prepare-data --reps road` writes one for your own users.
