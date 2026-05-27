import os
import codecs
import geopandas as gpd
import requests
import osmnx
import pandas as pd
import matplotlib.pyplot as plt
import contextily as cx
import osm2geojson
from matplotlib_scalebar.scalebar import ScaleBar
import geopandas as gpd

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

OVERPASS_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://overpass-api.de/",
}


def overpass_call(query, servers=None, headers=None, timeout=120):
    if servers is None:
        servers = OVERPASS_SERVERS
    if headers is None:
        headers = OVERPASS_HEADERS
    last_exception = None
    for endpoint in servers:
        try:
            response = requests.post(endpoint, data={"data": query}, headers=headers, timeout=timeout)
            if response.status_code == 200:
                return response.text
            last_exception = requests.exceptions.HTTPError(
                f"Overpass server respond with status {response.status_code} from {endpoint}"
            )
        except requests.exceptions.RequestException as exc:
            last_exception = exc
    if last_exception is None:
        raise requests.exceptions.HTTPError("Overpass server request failed without a response")
    raise last_exception

def to_data_dict(
    ls_classes, area_name, diskpath, clip=False, crs=False, gdf_area=False
):
    """
    Converts OpenStreetMap XML raw data to a OSMNX graph and to a GeoDataFrame for each infrastructure class.

    Parameters:
    - ls_classes (list): A list of dictionaries representing each infrastructure class consisting of:
        - name (str): The name of the infrastructure class.
        - query (str): The overpass turbo query statement to download the data.
    - area_name (str): The name of the study area.
    - diskpath (str): The path to the directory where the data will be saved.
    - clip (bool): Whether to clip the data to the study area. Default is False. If False, data which reaches beyond the study area will be kept.
    - crs (bool): The coordinate reference system to reproject the data. Default is False.
    - gdf_area_crs (bool): The coordinate reference system of the study area. Default is False.

    Returns:
    - ls_classes (list): The updated list of dictionaries with the converted data.
    """

    for infr_class in ls_classes:
        # Query with overpass turbo query statement defined above and save to xml string
        if infr_class["query"]:  # if query is empty, dict_own query was not defined by user
            xml_string = overpass_call(infr_class["query"])

            # Create the directory if it doesn't exist
            os.makedirs(f"{diskpath}/osm_data", exist_ok=True)
            # This workaround has to be made because to query directly with osmnx is too intricate with long query statements
            with codecs.open(
                f'{diskpath}/osm_data/export_{infr_class["name"]}_osm2geojson.osm',
                "w",
                encoding="utf-8",
            ) as f:
                f.write(xml_string)

        try:
            # Create osmnx graph and nodes / edges gdfs from it
            G = osmnx.graph.graph_from_xml(
                f'{diskpath}/osm_data/export_{infr_class["name"]}_osm2geojson.osm',
                retain_all=True,
                simplify=False,
            )

            if clip:  # Clip and reproject graph to exact study area if clip is True
                G = osmnx.projection.project_graph(G, to_crs=crs)
                G = osmnx.truncate.truncate_graph_polygon(
                    G, gdf_area.unary_union, truncate_by_edge=True
                )  # Clip graph to exact study area

            infr_class["graph"] = G

            # Open again and create gdf from xml file, containing version and timestamp tags (should be the same like gdf_edges)
            with codecs.open(
                f'{diskpath}/osm_data/export_{infr_class["name"]}_osm2geojson.osm',
                "r",
                encoding="utf-8",
            ) as data:
                xml = data.read()

            geojson = osm2geojson.xml2geojson(
                xml, filter_used_refs=False, log_level="INFO"
            )
            infr_class["gdf_xml"] = gpd.GeoDataFrame.from_features(
                geojson, crs="EPSG:4326"
            )

            if clip:  # Clip and reproject gdf to exact study area if clip is True
                infr_class["gdf_xml"] = osmnx.projection.project_gdf(
                    infr_class["gdf_xml"], to_crs=crs
                )
                infr_class["gdf_xml"] = gpd.clip(
                    infr_class["gdf_xml"], gdf_area.geometry.buffer(1.5)
                )  # Clip to exact study area with buffer tolerance

            infr_class["gdf_xml"] = infr_class["gdf_xml"].loc[
                infr_class["gdf_xml"]["type"] != "node"
            ]

        except Exception:
            # If in an area no infrastructure of a certain class exists create an empty geodataframe with columns that would be analyzed
            infr_class["gdf_xml"] = gpd.GeoDataFrame(
                columns=[
                    "empty",
                    "version",
                    "timestamp",
                    "geometry",
                    "tags",
                    "infr_length",
                ],
                geometry="empty",
                crs=crs,
            )

            # Print message for info only
            if not infr_class["query"]:
                print("No own infrastructur class choosen!")
                continue
            print(f'There is no {infr_class["name"]} infrastructure in {area_name}!')

    return ls_classes

def plot_infr(dict_class, label, nominatim_area):
    """
    Plot infrastructure on a map (with contextily) and their orientation (with osmnx).

    Parameters:
    - dict_class: class with this keys:
        - graph: NetworkX graph representing the road network
        - color: Color to use for plotting the infrastructure
    - label: Name of the infrastructure
    - nominatim_area: Name string of the area where the infrastructure is located

    Returns:
    None
    """
    edges = osmnx.convert.graph_to_gdfs(dict_class["graph"], nodes=False, edges=True)
    if not edges.empty:
        _, ax = plt.subplots(figsize=(10, 10))
        # reproject due to contextily basemap
        edges.to_crs(epsg=3857).plot(ax=ax, color=dict_class["color"], linewidth=2)

        ax.set_title(
            f"{label} in {nominatim_area} and their orientation (in the cardinal directions)"
        )
        ax.set_axis_off()
        try:
            cx.add_basemap(
                ax,
                source="https://tile.opentopomap.org/{z}/{x}/{y}.png",
            )
        except Exception as ex:
            print(f"Warning: Basemap could not be loaded: {ex}")

        # Add scale bar
        scalebar = ScaleBar(1, location='lower right')  # 1 pixel = 1 meter
        ax.add_artist(scalebar)

        # calculate bearing and orientation 
        # https://osmnx--1106.org.readthedocs.build/en/1106/user-reference.html#osmnx.plot.plot_orientation
        G = osmnx.bearing.add_edge_bearings(dict_class["graph"])
        G = osmnx.convert.to_undirected(G)
        osmnx.plot.plot_orientation(
            G, figsize=(1.5, 1.5), color=dict_class["color"], linewidth=0.1
        )
    else:
        print(f"There are are no {label} in {nominatim_area}!")

#### OSM TAG HANDLING AND DEFAULT VALUES HANDLING ###
def add_osm_tag_columns(gdf, tag_column="tags"):
    """
    Extract selected OSM tags from the tags dictionary into dedicated columns.
    Missing attributes are filled with None.
    """
    tag_mapping = {
        "highway class": "highway",
        "maxspeed": "maxspeed",
        "surface": "surface",
        "smoothness": "smoothness",
        "tracktype": "tracktype",
    }

    gdf = gdf.copy()

    for new_column, tag_key in tag_mapping.items():
        gdf[new_column] = gdf[tag_column].apply(
            lambda tags, key=tag_key: tags.get(key) if isinstance(tags, dict) else None
        )

    return gdf

def get_default_tag_values(row, default_values_by_highway_class, default_values_track):
    """Get default values for OSM tags based on the highway class and track type of a given row.

    Args:
        row (_type_): _description_
        default_values_by_highway_class (_type_): _description_
        default_values_track (_type_): _description_

    Returns:
        _type_: _description_
    """
    highway_defaults = default_values_by_highway_class
    track_defaults = default_values_track
    highway_class = row.get("highway class")
    tracktype = row.get("tracktype")

    if pd.notna(highway_class) and str(highway_class) == "track":
        if pd.notna(tracktype):
            track_default = track_defaults.get(str(tracktype), highway_defaults["track"])
            return {
                "highway class": "track",
                "maxspeed": track_default["maxspeed"],
                "surface": track_default["surface"],
                "smoothness": track_default["smoothness"],
            }
        track_default = highway_defaults["track"]
        return {
            "highway class": "track",
            "maxspeed": track_default["maxspeed"],
            "surface": track_default["surface"],
            "smoothness": track_default["smoothness"],
        }

    highway_default = highway_defaults.get(
        str(highway_class), highway_defaults["unclassified"]
    )
    return {
        "highway class": str(highway_class) if pd.notna(highway_class) else "unclassified",
        "maxspeed": highway_default["maxspeed"],
        "surface": highway_default["surface"],
        "smoothness": highway_default["smoothness"],
    }

### OSM TAG SCORING ###
def get_osm_tag_score(category, tag_value, osm_tag_scores, default=None):
    """Get the score for a given OSM tag value based on the provided scoring dictionary."""
    if tag_value is None or pd.isna(tag_value):
        return default
    return osm_tag_scores.get(category, {}).get(str(tag_value), default)

def calculate_score_and_missing_tags(row, osm_tag_scores, default_values_by_highway_class, default_values_track):
    """"Calculate the score for a given row based on OSM tags and identify missing tags.
    Args:        row (pd.Series): A row from the GeoDataFrame containing OSM tags.
        osm_tag_scores (dict): A dictionary containing the scoring for each OSM tag category and value.
        default_values_by_highway_class (dict): A dictionary containing default values for each highway class.
        default_values_track (dict): A dictionary containing default values for track types.
    Returns:        pd.Series: A Series containing the calculated score and a list of missing tags.
    """

    defaults = get_default_tag_values(row, default_values_by_highway_class, default_values_track)
    missing_tags = []
    score = 0

    for category in ["highway class", "maxspeed", "surface", "smoothness"]:
        value = row.get(category)
        if value is None or pd.isna(value):
            missing_tags.append(category)
            value = defaults.get(category)

        score += get_osm_tag_score(category, value, osm_tag_scores, default=0)

    return pd.Series({"score": score, "missing_tags": missing_tags})


### STRESS LEVEL CLASSIFICATION ###
def classify_score(score):
    """Classify the score into classes based on defined thresholds.

    Args:
        score (_type_): _description_

    Returns:
        _type_: _description_
    """
    if pd.isna(score):
        return None
    if score <= 10:
        return 4
    if score <= 20:
        return 3
    if score <= 30:
        return 2
    return 1


