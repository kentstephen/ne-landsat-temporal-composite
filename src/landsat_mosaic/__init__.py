"""Build the New England leaf-on Landsat mosaic pyramid.

Pipeline, in run order: fetch_catalog, planner, runner (driven by batch),
qa_level0, tag, export_zarr, pyramid, source_pass, store_source,
source_pyramid, gc_store, upload, remote_check. grid and init_store are the
shared layer; state_boundary, wildlands, water and supplemental build the
vector layers that ship beside the store; tiles renders the pyramid for
mosaic-viewer.py. Everything else measured
something that shaped a decision (see docs/10-scripts.md).
"""
