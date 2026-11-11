"""Geodata conversion with GDAL/OGR: anything OGR can read -> GeoJSON (EPSG:4326)."""
from __future__ import annotations

import json
import logging
import os
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings

log = logging.getLogger("pkdb.geo")

GEO_EXTENSIONS = {
    ".geojson", ".json", ".gpx", ".kml", ".kmz", ".gpkg", ".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix",
    ".sbn", ".sbx", ".zip", ".fgb", ".gml", ".topojson", ".osm", ".pbf", ".csv", ".tab", ".mif", ".mid",
    ".dxf", ".sqlite", ".gdb", ".ods", ".xlsx",
}
SHP_PARTS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"}
OPENABLE_IN_ZIP = {".shp", ".geojson", ".json", ".gpx", ".kml", ".gpkg", ".fgb", ".gml", ".topojson", ".csv",
                   ".tab", ".mif", ".dxf", ".sqlite"}
NAME_KEYS = ("name", "Name", "NAME", "title", "Title", "TITLE", "bezeichnung", "Bezeichnung", "BEZEICHNUNG",
             "label", "Label", "LABEL", "NAME_DE", "name_de", "desc", "description", "id", "ID")
# GPX point layers duplicate the track/route geometry; skip them if there are other layers
SKIP_IF_OTHERS = {"track_points", "route_points"}


@dataclass
class GeoLayer:
    name: str
    features: list[dict] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "count": len(self.features), "truncated": self.truncated,
                "geojson": {"type": "FeatureCollection", "features": self.features}}


@dataclass
class GeoDataset:
    key: str                  # group key (file stem / file name) used to match user metadata
    files: list[str]
    layers: list[GeoLayer] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "files": self.files, "error": self.error,
                "layers": [lyr.as_dict() for lyr in self.layers]}


def is_geo_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in GEO_EXTENSIONS


def group_files(paths: list[Path]) -> dict[str, list[Path]]:
    """Group uploaded files into datasets. Shapefile parts are grouped by their stem."""
    groups: dict[str, list[Path]] = {}
    stems_with_shp = {p.stem.lower() for p in paths if p.suffix.lower() == ".shp"}
    for p in paths:
        ext = p.suffix.lower()
        if ext in SHP_PARTS and p.stem.lower() in stems_with_shp:
            key = p.stem + ".shp"
        else:
            key = p.name
        groups.setdefault(key, []).append(p)
    # drop orphan shapefile sidecars (e.g. a .prj without .shp)
    return {k: v for k, v in groups.items()
            if not (len(v) == 1 and v[0].suffix.lower() in SHP_PARTS - {".shp"})}


def _sources_for(group: list[Path]) -> list[str]:
    """Return GDAL dataset names to open for one group."""
    shp = [p for p in group if p.suffix.lower() == ".shp"]
    if shp:
        return [str(shp[0])]
    p = group[0]
    ext = p.suffix.lower()
    if ext in (".zip", ".kmz"):
        try:
            with zipfile.ZipFile(p) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/") and "__MACOSX" not in n]
        except zipfile.BadZipFile:
            return [str(p)]
        inner = [n for n in names if Path(n).suffix.lower() in OPENABLE_IN_ZIP]
        if ext == ".kmz" and not inner:
            return [str(p)]
        return [f"/vsizip/{p}/{n}" for n in inner] or [str(p)]
    return [str(p)]


def _feature_name(props: dict) -> str:
    for k in NAME_KEYS:
        v = props.get(k)
        if v not in (None, ""):
            return str(v)[:300]
    return ""


def _convert_source(src: str) -> list[GeoLayer]:
    from osgeo import gdal, ogr  # imported lazily; only available in the Docker image

    gdal.UseExceptions()
    ds = gdal.OpenEx(src, gdal.OF_VECTOR | gdal.OF_READONLY)
    if ds is None:
        raise ValueError("GDAL could not open the file")
    layer_names = []
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(i)
        if lyr.GetGeomType() == ogr.wkbNone:
            continue
        if lyr.GetFeatureCount() == 0:
            continue
        layer_names.append((lyr.GetName(), lyr.GetSpatialRef() is not None))
    if len(layer_names) > 1:
        others = [n for n, _ in layer_names if n not in SKIP_IF_OTHERS]
        if others:
            layer_names = [(n, s) for n, s in layer_names if n not in SKIP_IF_OTHERS]

    out: list[GeoLayer] = []
    limit = settings.max_features_per_layer
    for name, has_srs in layer_names:
        tmp = f"/vsimem/{uuid.uuid4().hex}.geojson"
        opts = gdal.VectorTranslateOptions(
            format="GeoJSON", layers=[name], dstSRS="EPSG:4326",
            srcSRS=None if has_srs else "EPSG:4326",
            layerCreationOptions=["RFC7946=YES", "WRITE_NAME=NO"],
            skipFailures=True,
        )
        try:
            gdal.VectorTranslate(tmp, ds, options=opts)
            f = gdal.VSIFOpenL(tmp, "rb")
            try:
                gdal.VSIFSeekL(f, 0, 2)
                size = gdal.VSIFTellL(f)
                gdal.VSIFSeekL(f, 0, 0)
                data = gdal.VSIFReadL(1, size, f)
            finally:
                gdal.VSIFCloseL(f)
        finally:
            gdal.Unlink(tmp)
        fc = json.loads(data)
        feats = [ft for ft in fc.get("features", []) if ft.get("geometry")]
        layer = GeoLayer(name=name)
        if len(feats) > limit:
            feats = feats[:limit]
            layer.truncated = True
        for ft in feats:
            ft["properties"] = {k: v for k, v in (ft.get("properties") or {}).items() if v is not None}
        layer.features = feats
        if feats:
            out.append(layer)
    return out


def convert_group(key: str, group: list[Path]) -> GeoDataset:
    ds = GeoDataset(key=key, files=[p.name for p in group])
    try:
        ext = group[0].suffix.lower()
        if ext in (".geojson", ".json") and len(group) == 1 and not _gdal_available():
            ds.layers = _parse_geojson_fallback(group[0])
            return ds
        sources = _sources_for(group)
        for src in sources:
            for lyr in _convert_source(src):
                if len(sources) > 1 and lyr.name != Path(src).stem:
                    lyr.name = f"{Path(src).name} / {lyr.name}"
                ds.layers.append(lyr)
        if not ds.layers:
            ds.error = "No vector features found"
    except Exception as e:  # noqa: BLE001
        log.warning("Conversion of %s failed: %s", key, e)
        ds.error = f"Could not read geodata: {e}"
    return ds


def convert_files(paths: list[Path]) -> list[GeoDataset]:
    return [convert_group(k, g) for k, g in group_files(paths).items()]


def _gdal_available() -> bool:
    try:
        import osgeo  # noqa: F401
        return True
    except ImportError:
        return False


def _parse_geojson_fallback(path: Path) -> list[GeoLayer]:
    """Used only when GDAL is missing (local development)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        feats = data.get("features", [])
    elif data.get("type") == "Feature":
        feats = [data]
    else:
        feats = [{"type": "Feature", "geometry": data, "properties": {}}]
    return [GeoLayer(name=path.stem, features=[f for f in feats if f.get("geometry")])]


def feature_name(props: dict) -> str:
    return _feature_name(props or {})


def ensure_dir(*parts: str) -> Path:
    p = Path(settings.data_dir, *parts)
    os.makedirs(p, exist_ok=True)
    return p
