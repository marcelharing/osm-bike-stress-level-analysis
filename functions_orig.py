import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
import codecs
from datetime import datetime
import geopandas as gpd
import numpy as np
from tqdm import tqdm
import requests
import osmnx
import pandas as pd
import networkx as nx
import matplotlib.pyplot as plt
from dateutil.relativedelta import relativedelta
import plotly.graph_objects as go
import contextily as cx
import osm2geojson
from shapely import line_merge
from shapely import unary_union 
from shapely.geometry import LineString
from matplotlib_scalebar.scalebar import ScaleBar

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


#####  FUNCTIONS EXTRINSIC ANALYSIS  #####
def calculate_infr_length(row):
    """
    Calculates the actual infrastructure length based on the given row by multiplying the geom_length by 2
    if a key cycleway, cycleway:both, cycleway:left and cycleway:right is in the attributes.
    This features in OSM share one geometry, but are actually two separate infrastructures.
    Args:
      row (pandas.Series): Row containing the data for the calculation and a column named 'geom_length' as feature geometry length.
    Returns:
      pandas.Series: The updated row with the 'infr_length' calculated.
    """

    def check_valid_values(tag_dict, key):
        """
        Inner function, that checks if the value of a specific key in the tag dictionary is valid.
        Args:
          tag_dict (dict): The dictionary containing the tags.
          key (str): The key to check.
        Returns:
          bool: True if the value is valid, False otherwise.
        """
        if (
            tag_dict[key] == "lane"
            or tag_dict[key] == "opposite_lane"
            or tag_dict[key] == "track"
            or tag_dict[key] == "opposite_track"
        ):
            return True
        else:
            return False

    tag_dict = row.loc["tags"]

    # Check for tag cycleway
    if "cycleway" in tag_dict:
        if check_valid_values(tag_dict, "cycleway"):
            row.loc["infr_length"] = row.loc["geom_length"] * 2
            return row

    # Check for tag cycleway:both
    if "cycleway:both" in tag_dict:
        if check_valid_values(tag_dict, "cycleway:both"):
            row.loc["infr_length"] = row.loc["geom_length"] * 2
            return row

    # Check for tag cycleway:left AND cycleway:right
    if "cycleway:left" in tag_dict and "cycleway:right" in tag_dict:
        if check_valid_values(tag_dict, "cycleway:left") and check_valid_values(
            tag_dict, "cycleway:right"
        ):
            row.loc["infr_length"] = row.loc["geom_length"] * 2
            return row

    # In all other cases, the infrastructure length is the same as the geometry length
    row.loc["infr_length"] = row.loc["geom_length"]

    return row


def to_linestring(gdf):
    """
    Converts the input GeoDataFrame to contain only LineString geometries.
    It turnes (explodes) MultiLineStrings into LineStrings and Polygons into LineStrings using the boundary.
    MultiPolygons often appear when Linestrings are connected and interpreted as ring, 
    Multilinestrings appear when Linestrings are not directly connected as a line.
    All other geometry types are dropped.

    Parameters:
    gdf (GeoDataFrame): The input GeoDataFrame.

    Returns:
    GeoDataFrame: The modified GeoDataFrame containing only LineString geometries.
    """

    # Turn (explode) multilinestrings into linestrings
    gdf = gdf.explode(index_parts=False, ignore_index=True)

    # Convert possible Polygons to LineStrings using the boundary
    gdf.loc[gdf.type == "Polygon", "geometry"] = gdf.loc[
        gdf.type == "Polygon", "geometry"
    ].boundary

    # Drop all other geometry types than LineString
    index_nolinestring = gdf.loc[
        gdf.type != "LineString"
    ].index  # get index of all non LineString geometries
    rows_before = gdf.shape[0]

    if not (gdf.type == "LineString").all():
        gdf.drop(index_nolinestring, inplace=True)

        # Calculate with index before and index after how many rows were dropped
        rows_after = gdf.shape[0]
        print(f"{rows_before - rows_after} row(s) dropped which are no Linestrings.")

    return gdf.reset_index(drop=True)  # reset index after dropping rows


def clean_turnuse(row, gdf_radvis):
    """
    Check if the 'USE_FROM_ID' and 'USE_TO_ID' values connects each to a bicycle infrastructure; 
    show therefore if row exist in the 'gdf_radvis_clip' DataFrame.

    Parameters:
    - row: pandas Series or DataFrame row containing the 'USE_FROM_ID' and 'USE_TO_ID' values.
    Returns:
    - True if both 'USE_FROM_ID' and 'USE_TO_ID' values exist in gdf_radvis_clip 'gip_lu_id' row, False otherwise.
    """
    id_from = row.loc["USE_FROM_ID"]
    id_to = row.loc["USE_TO_ID"]

    if (
        id_from in gdf_radvis["gip_lu_id"].values
        and id_to in gdf_radvis["gip_lu_id"].values
    ):
        return True
    else:
        return False


def classify_infr_class_gip(row):
    """
    Classifies the infrastructure class (Cycle Lanes, Cycle Tracks, Calm Traffic Ways) based on column values of 'bike_tow' and 'bike_bkw'.
    Assigns 'infr_class' column with the classification or pd.NA if the values conflicts between 'bike_tow' and 'bike_bkw'.
    Args:
        row (pandas.Series): The row containing the 'bike_tow' and 'bike_bkw' columns.
    Returns:
        pandas.Series: The row with the 'infr_class' column updated based on the classification.
    """
    cycle_tracks = [
        "GRW_M",
        "GRW_MO",
        "GRW_T",
        "GRW_TO",
        "MTB",
        "RFUE",
        "RW",
        "RWO",
        "SGT",
        "TRR",
        "SCHUTZWEG_RFUE",
        "GRW_MV",
        "GRW_MOV",
    ]
    cycle_lanes = ["MZSTR", "RF"]
    calm_traffic_ways = [
        "BGZ",
        "FRS",
        "FUZO",
        "FUZO_N",
        "RVW",
        "VK_BE",
        "WSTR",
        "WSTR_N",
    ]

    # Different values in straight ahead and opposite direction which conflicts between the classes Cycle Lanes, Cycle Tracks, Calm Traffic Ways
    conditions = [
        (row.loc["bike_tow"] in cycle_tracks and row.loc["bike_bkw"] in cycle_lanes),
        (
            row.loc["bike_tow"] in cycle_tracks
            and row.loc["bike_bkw"] in calm_traffic_ways
        ),
        (
            row.loc["bike_tow"] in cycle_lanes
            and row.loc["bike_bkw"] in calm_traffic_ways
        ),
        (row.loc["bike_tow"] in cycle_lanes and row.loc["bike_bkw"] in cycle_tracks),
        (
            row.loc["bike_tow"] in calm_traffic_ways
            and row.loc["bike_bkw"] in cycle_lanes
        ),
        (
            row.loc["bike_tow"] in calm_traffic_ways
            and row.loc["bike_bkw"] in cycle_tracks
        ),
    ]
    if any(conditions):
        print(
            f"feature with id {row.loc['idf_use_id']} seems to have conflicting values in bike_tow and bike_bkw"
        )
        row.loc["infr_class"] = np.nan
    # Else both values are in the same class, so we can assign the infr_class
    elif row.loc["bike_tow"] in cycle_tracks or row.loc["bike_bkw"] in cycle_tracks:
        row.loc["infr_class"] = "Cycle Tracks"
    elif row.loc["bike_tow"] in cycle_lanes or row.loc["bike_bkw"] in cycle_lanes:
        row.loc["infr_class"] = "Cycle Lanes"
    elif (
        row.loc["bike_tow"] in calm_traffic_ways
        or row.loc["bike_bkw"] in calm_traffic_ways
    ):
        row.loc["infr_class"] = "Calm Traffic Ways"
    # Any other (edge) case, assign None
    else:
        row.loc["infr_class"] = np.nan
    return row


def create_node_edge_model(
    gdf_radvis_reclass, gdf_turnuse_cleaned, gdf_gip_nodes, mostturnuses=False
):
    """
    Creates a new edge-node model.
    If more than two edges connects to an node/intersection, the node is connected to all edges.
    If only two edges connects, the edges are connected right with each other

    Args:
      gdf_radvis_reclass (GeoDataFrame): GeoDataFrame containing the radvis reclassified data.
      gdf_turnuse_cleaned (GeoDataFrame): GeoDataFrame containing the cleaned turnuse data.
      gdf_gip_nodes (GeoDataFrame): GeoDataFrame containing the GIP nodes data.
      mostturnuses (bool): If True, the coordinates of the node with the most turnuses are returned.

    Returns:
      gdf_connected_node_radvis (GeoDataFrame): GeoDataFrame representing the new edge-node model.
      if mostturnuses is True:
      coords_mostturnuses (tuple): Tuple containing the coordinates of the node with the
    """

    #### DICT TURNUSE CONNECTION AMOUNT AND DICT NODES  COORDINATES ####
    # Calculate how much turnuse connections each intersection node has and save it in a dictionary with node id as key
    dict_turnuse_connection_amount = {}
    for _, line in gdf_turnuse_cleaned.iterrows():
        # If already in dict_help_turnuse add 1 for further connection
        if line.VIA_NODE_ID in dict_turnuse_connection_amount.keys():
            dict_turnuse_connection_amount[line.VIA_NODE_ID] += 1
        else:
            dict_turnuse_connection_amount[line.VIA_NODE_ID] = 1

    # Dict of intersection nodes geometry/coordinates: 
    # built dict to call from every node the geometry/coordinates with its OBJECTID
    dict_help_nodes = {}
    for _, node in gdf_gip_nodes.iterrows():
        node_coor = node.geometry.coords[0]
        dict_help_nodes[node.OBJECTID] = node_coor

    #### NEW MODEL CONSTRUCTION ####
    # Get intersection node geometry and built new LineString to it with first and last point of every turnuse LineString
    # As a result, every edge is then connected with the node at intersections (node-edge model)
    connected_lines = []
    attributes = []
    for _, line in gdf_turnuse_cleaned.iterrows():
        if line.VIA_NODE_ID in dict_help_nodes.keys():
            first_coord = line.geometry.coords[0]
            last_coord = line.geometry.coords[-1]
            key = line.VIA_NODE_ID
            node_coord = dict_help_nodes[key]

            # If there are only 1 or 2 connections via a node, connect edges directly (because Turnuse line is then also
            # connected directly, connecting over intersection node would take a detour) otherwise connect them via intersection node
            # For connections, create new LineString and add original Turnuse attributes

            if (
                dict_turnuse_connection_amount[line.VIA_NODE_ID] == 1
                or dict_turnuse_connection_amount[line.VIA_NODE_ID] == 2
            ):
                new_line = LineString([first_coord, last_coord])
                connected_lines.append(new_line)
                attributes.append(
                    {
                        "VIA_NODE_ID": line.VIA_NODE_ID,
                        "USE_TO_ID": line.USE_TO_ID,
                        "USE_FROM_ID": line.USE_FROM_ID,
                        "TURNUSE_OBJECTID": line.OBJECTID,
                        "TIMESTAMP": line.TIMESTAMP,
                        "infr_class": "Turnuse",
                    }
                )

            else:
                new_line = LineString([first_coord, node_coord])
                new_line2 = LineString([last_coord, node_coord])
                connected_lines.append(new_line)
                attributes.append(
                    {
                        "VIA_NODE_ID": line.VIA_NODE_ID,
                        "USE_TO_ID": line.USE_TO_ID,
                        "USE_FROM_ID": line.USE_FROM_ID,
                        "TURNUSE_OBJECTID": line.OBJECTID,
                        "TIMESTAMP": line.TIMESTAMP,
                        "infr_class": "Turnuse",
                    }
                )
                connected_lines.append(new_line2)
                attributes.append(
                    {
                        "VIA_NODE_ID": line.VIA_NODE_ID,
                        "USE_TO_ID": line.USE_TO_ID,
                        "USE_FROM_ID": line.USE_FROM_ID,
                        "TURNUSE_OBJECTID": line.OBJECTID,
                        "TIMESTAMP": line.TIMESTAMP,
                        "infr_class": "Turnuse",
                    }
                )

    # All linestrings in list as new gdf
    gdf_node_connections = gpd.GeoDataFrame(
        geometry=connected_lines, crs=gdf_turnuse_cleaned.crs
    )

    # Add attributes to the GeoDataFrame
    for attr_name in attributes[0].keys():
        gdf_node_connections[attr_name] = [attr[attr_name] for attr in attributes]

    # Drop duplicates because from every turnuse a new connection line was made and edges are connected to > 2 turnuse lines
    gdf_node_connections.drop_duplicates(subset="geometry", inplace=True)

    # Stack connections and radvis edges together to have new edge-node model
    gdf_connected_node_radvis = gpd.GeoDataFrame(
        pd.concat([gdf_radvis_reclass, gdf_node_connections], ignore_index=True),
        crs=[gdf_radvis_reclass, gdf_node_connections][0].crs,
    )

    #### COORDINATES BIGGEST INTERSECTION ####
    # Get coordinates with the biggest intersection ( = most turnuse links) from dict_turnuse_connection_amount
    max_key = max(
        dict_turnuse_connection_amount, key=dict_turnuse_connection_amount.get
    )
    gdf_mostturnuses = gdf_turnuse_cleaned[
        gdf_turnuse_cleaned["VIA_NODE_ID"] == int(max_key)
    ]
    gdf_mostturnuses = osmnx.projection.project_gdf(
        gdf_mostturnuses, to_crs="EPSG:4326"
    )
    coords_mostturnuses = gdf_mostturnuses.iloc[0].geometry.coords[0]

    if mostturnuses:
        return gdf_connected_node_radvis, coords_mostturnuses
    else:
        return gdf_connected_node_radvis


def calculate_dist_statistics(df, column):
    """
    Calculates the median, 0.25 quantile, and 0.1 quantile of a given column in a DataFrame.
    Parameters:
    df (pandas.DataFrame): The DataFrame containing the data.
    column (str): The name of the column to calculate the statistics for.
    Returns:
    list: A list containing the median, 0.25 quantile, and 0.1 quantile of the column.
    """
    return [df[column].median(), df[column].quantile(0.25), df[column].quantile(0.1)]


def get_dangling_nodes(network_edges, network_nodes):
    """
    CREDITS: Vierø A. R., Vybornova A., Szell M.: BIKEDNA; AGPL 3.0; 
    https://github.com/anerv/BikeDNA/tree/main

    Return all dangling (degree one) nodes in a network
    Assumes an undirected network - if two parallel nodes representing a two-way street end in the same
    node, it will not be considered dangling.

    Arguments:
        network edges (gdf): gdf with network edges indexed by their start and end nodes
        network nodes (gdf): gdf with network nodes

    Returns:
        dangling_nodes (gdf): geodataframe with all dangling nodes
    """

    edges = network_edges.copy(deep=True)
    nodes = network_nodes.copy(deep=True)

    if "u" not in edges.columns:

        all_node_occurences = (
            edges.reset_index().u.to_list() + edges.reset_index().v.to_list()
        )

    else:

        all_node_occurences = edges.u.to_list() + edges.v.to_list()

    dead_ends = [x for x in all_node_occurences if all_node_occurences.count(x) == 1]

    dangling_nodes = nodes[nodes.index.isin(dead_ends)]

    return dangling_nodes


def get_subgraphs(graph):
    """
    Get disconnected components of a graph as subgraphs and sort them based on their length.

    Parameters:
    graph (networkx.Graph): The input graph.
    Returns:
    list: A list of tuples, where each tuple contains a subgraph and its length of geometry.
    """

    def tags_to_dict(row):
        """Put tags saved in columns into a own column consisting of a dictionary."""
        row["tags"] = row.to_dict()
        return row

    # Get all disconnected components as own subgraphs
    ls_G_components = [graph.subgraph(C).copy() for C in nx.connected_components(graph)]
    # Get edges of subgraphs
    ls_edge_components = [
        osmnx.graph_to_gdfs(c, nodes=False, edges=True) for c in ls_G_components
    ]

    # Replace edges with a tuple containing the edges and the total infrastructure length of the component
    # Calculate the length of the geometry with 'calculate_infr_length' func., create a new column with tags as dictionary as preparation
    ls_edge_components_with_length = []
    for e in ls_edge_components:
        e = e.apply(tags_to_dict, axis=1)
        e["geom_length"] = e["geometry"].length
        e = e.apply(calculate_infr_length, axis=1)  # adds infr_length column
        l_sum = e["infr_length"].sum()
        ls_edge_components_with_length.append((e, l_sum))

    # Sort list based on component length
    ls_edge_components_with_length = [
        tup
        for tup in sorted(
            ls_edge_components_with_length, key=lambda x: x[1], reverse=True
        )
    ]

    return ls_edge_components_with_length


def find_adjacent_components(components, edge_id, buffer_dist, crs):
    """
    CREDITS: Vierø A. R., Vybornova A., Szell M.: BIKEDNA; AGPL 3.0; 
    https://github.com/anerv/BikeDNA/tree/main

    Find edges in different (unconnected) components that are within a specified distance from each other.

    Arguments:
        components (list): list with network components (networkx graphs)
        edge_id (str): name of column with unique edge id
        buffer_dist (numeric): max distance for which edges in different components are considered 'adjacent'
        crs (str): crs to use when computing distances between edges

    Returns:
        all_results (dict): dictionary with the ids for all edge pairs identifying as overlapping based on their buffers, 
        and the centroid of the buffer intersection
    """

    edge_list = []

    for i, c in enumerate(components):

        if len(c.edges) > 0:

            edges = osmnx.graph_to_gdfs(c, nodes=False)

            edges["component"] = i

            edge_list.append(edges)

    component_edges = pd.concat(edge_list)

    component_edges = component_edges.set_crs(crs)

    # Buffer component edges and find overlapping buffers
    component_edges_buffer = component_edges.copy()
    component_edges_buffer.geometry = component_edges_buffer.geometry.buffer(
        buffer_dist / 2
    )

    component_buffer_sjoin = gpd.sjoin(component_edges_buffer, component_edges_buffer)
    # Drop matches between edges on the same component
    intersecting_buffer_components = component_buffer_sjoin.loc[
        component_buffer_sjoin.component_left != component_buffer_sjoin.component_right
    ].copy()

    left_ids = intersecting_buffer_components[edge_id + "_left"].to_list()
    right_ids = intersecting_buffer_components[edge_id + "_right"].to_list()

    adjacent_edges_all = list(zip(left_ids, right_ids))
    adjacent_edges_all = [set(a) for a in adjacent_edges_all]

    # Remove duplicaties
    adjacent_edges = []
    for item in adjacent_edges_all:
        if item not in adjacent_edges:
            adjacent_edges.append(item)

    adjacent_edges = [tuple(a) for a in adjacent_edges]

    all_results = {}

    for i in range(len(adjacent_edges)):
        intersection = gpd.overlay(
            component_edges_buffer.loc[
                component_edges_buffer[edge_id] == adjacent_edges[i][0]
            ],
            component_edges_buffer.loc[
                component_edges_buffer[edge_id] == adjacent_edges[i][1]
            ],
            how="intersection",
        )

        geom = intersection.geometry.centroid.values[0]

        results = {}
        results[edge_id + "_left"] = adjacent_edges[i][0]
        results[edge_id + "_right"] = adjacent_edges[i][1]
        results["geometry"] = geom

        all_results[i] = results

    return all_results


def calculate_attribute_presence(
    gdf_all, name, ls_osm_attributes_select, ls_osm_attributes_analyze
):
    """
    Calculate the attribute presence for a given GeoDataFrame originated from OSMNX 
    (i.e. OSM attribute keys have a own column with values as elements).

    Takes a list of preselected OSM attributes (as key=value), line features with this attributes act as starting point for the presence analysis.
    Takes a second list with attributes (as key) to analyze its presence (i.e if any value is present).
    It gives back two GDFs, one with line features with the attribute to analyze, 
    the other with total line features (defined with preselected attributes).
    Afterwards the proportion could be calculated.

    Parameters:
    - gdf_all (GeoDataFrame): The input GeoDataFrame containing all the data.
    - name (str): The name of the dataset for error messages.
    - ls_osm_attributes_select (list): A list of selected OSM attributes. Format ['key1=value1, 'key2=value1', 'key3=value2']
    - ls_osm_attributes_analyze (list): A list of attributes to analyze. Format ['key1', 'key2', 'key3']

    Returns:
    - list: A list with two GDFs. One with features with the analyzed attribute, one GDF with all featues/ features with preselected attributes.

    Raises:
    - KeyError: If at least one of the attributes being evaluated does not exist in the infrastructure dataset.
    """
    try:
        # If ls_osm_attributes_select empty, evaluate the whole dataset (no preselection)
        if not ls_osm_attributes_select[0]:
            gdf_selected_attributes = gdf_all
            # Drop if line feature has nan in all subset columns (list osm_attributes to analyze) to have amount of features with presence
            gdf_with_attribute = gdf_selected_attributes.dropna(
                subset=ls_osm_attributes_analyze, how="all"
            )
            # Calculate proportion and round
            return [gdf_with_attribute, gdf_selected_attributes]

        else:
            ls_gdf_attribute = []
            # Preselect for each attribute in ls_osm_attributes_select with key and value of the attribute and append to a list
            # Each key has its own column with values as elements, so can be selected with the loc property.
            for osm_attribute_select in ls_osm_attributes_select:
                osm_key = osm_attribute_select.split("=")[0]
                osm_value = osm_attribute_select.split("=")[1]
                gdf_attribute = gdf_all.loc[gdf_all[osm_key] == osm_value]
                ls_gdf_attribute.append(gdf_attribute)

            # Stack all gdfs with each select_attribute together in one gdf
            gdf_selected_attributes_stacked = gpd.GeoDataFrame(
                pd.concat(ls_gdf_attribute, ignore_index=True),
                crs=ls_gdf_attribute[0].crs,
            )
            # If stacked gdf is empty (i.e. selected attributes are not present in dataset), print info and return empty GDF
            if gdf_selected_attributes_stacked.empty:
                print(
                    f"Preselection empty. Selected attribute(s) doesn't exist in {name} infrastructure dataset."
                )
                return [
                    gpd.GeoDataFrame(
                        columns=["empty"], geometry="empty", crs="EPSG:4326"
                    ),
                    gpd.GeoDataFrame(
                        columns=["empty"], geometry="empty", crs="EPSG:4326"
                    ),
                ]

            # Drop if line feature has nan in all subset columns (list osm_attributes to analyze) to have amount of features with presence
            gdf_with_attribute = gdf_selected_attributes_stacked.dropna(
                subset=ls_osm_attributes_analyze, how="all"
            )
            # Calculate proportion and round
            return [gdf_with_attribute, gdf_selected_attributes_stacked]

    except KeyError:
        # If key error (i.e. no analyze attribute present at all in dataset) print info and return empty GDF
        print(
            f"At least one of the attribute you are trying to evaluate does not exist in {name} infrastructure dataset."
        )
        return [
            gpd.GeoDataFrame(columns=["empty"], geometry="empty", crs="EPSG:4326"),
            gpd.GeoDataFrame(columns=["empty"], geometry="empty", crs="EPSG:4326"),
        ]


def get_class_lengths(gdf, infr_class, lengthcolumn):
    """
    Prepare and round the total length for a specific infrastructure class in a GeoDataFrame.

    Parameters:
    gdf (GeoDataFrame): The input GeoDataFrame containing the infrastructure data.
    infr_class (str): The infrastructure class to calculate the length for. Use 'Total' to calculate the total length of all classes.
    lengthcolumn (str): The name of the column in the GeoDataFrame that contains the length values.

    Returns:
    float: The total length of the specified infrastructure class in kilometers and rounded.
    """
    if infr_class == "Total":
        gdf_class = gdf
    else:
        gdf_class = gdf[gdf["infr_class"] == infr_class]

    length = round(gdf_class[lengthcolumn].sum() / 1000, 3)

    return length


def create_grid_geometry(gdf, cell_size):
    """
    CREDITS: Vierø A. R., Vybornova A., Szell M.: BIKEDNA; AGPL 3.0; 
    https://github.com/anerv/BikeDNA/tree/main

    Creates a geodataframe with grid cells covering the area specificed by the input gdf

    Arguments:
        gdf (gdf): geodataframe with a polygon/polygons defining the study area
        cell_size (numeric): width of the grid cells in units used by gdf crs

    Returns:
        grid (gdf): gdf with grid cells in same crs as input data
    """
    geometry = gdf["geometry"].unary_union
    geometry_cut = osmnx.utils_geo._quadrat_cut_geometry(
        geometry, quadrat_width=cell_size
    )

    grid = gpd.GeoDataFrame(geometry=[geometry_cut], crs=gdf.crs)

    grid = grid.explode(index_parts=False, ignore_index=True)

    # Create arbitraty grid id col
    grid["grid_id"] = grid.index

    return grid


def get_grid_density(gdf, infra, length, grid):
    """
    Group by 'grid_id' and calculate the grid density (i.e. the sum of infrastructure for each cell).
    based on the given infrastructure and group by cell.

    Parameters:
    gdf (DataFrame): The input DataFrame containing grid data.
    infra (str): The class of infrastructure to select from GDF. Use 'Total' for all infrastructure types.
    length (str): The column  with length values.

    Returns:
    DataFrame: The input DataFrame with an additional column representing the grid density.
    """

    # Group by grid id and sum the infrastructure length for each cell up
    if infra == "Total":
        length_pergrid = gdf.groupby(by="grid_id")[length].sum().to_frame()
    else:
        length_pergrid = (
            gdf.loc[gdf["infr_class"] == infra]
            .groupby(by="grid_id")[length]
            .sum()
            .to_frame()
        )

    # Merge the serial with infrastructure length with the grid
    return grid.merge(length_pergrid, on="grid_id")


def get_grid_length_diff(grid_w_lengths_osm, grid_w_lengths_gip, basegrid):
    """
    Calculate the absolute difference in length between OSM and GIP features for each cell in a grid grid.

    Parameters:
    - grid_w_lengths_osm (DataFrame): GeoDataFrame containing the length of OSM features for each grid cell in a column 'infr_length'.
    - grid_w_lengths_gip (DataFrame): GeoDataFrame containing the length of GIP features for each grid cell in a column 'geom_length'.
    - basegrid (DataFrame): GeoDataFrame containing grid cells and a column 'grid_id' with unique IDs for each cell.

    Returns:
    - grid_length_diff (DataFrame): GeoDataFrame containing the absolute difference 
    in length between OSM and GIP features for each cell in the grid.
    """

    # Create new grid with length of gip features and fill NaN values with 0
    grid_length_diff = basegrid.merge(
        grid_w_lengths_gip[["geom_length", "grid_id"]], on="grid_id", how="left"
    )
    grid_length_diff = grid_length_diff.rename(columns={"geom_length": "length_gip"})
    grid_length_diff["length_gip"] = grid_length_diff["length_gip"].fillna(0)

    # Create new grid with length of osm features and fill NaN values with 0
    grid_length_diff = grid_length_diff.merge(
        grid_w_lengths_osm[["infr_length", "grid_id"]], on="grid_id", how="left"
    )
    grid_length_diff = grid_length_diff.rename(columns={"infr_length": "length_osm"})
    grid_length_diff["length_osm"] = grid_length_diff["length_osm"].fillna(0)

    # Calculate absolute difference of the both length for each cell
    grid_length_diff["diff_absolute"] = round(
        (grid_length_diff["length_gip"] - grid_length_diff["length_osm"])
    )

    return grid_length_diff


def fill_empty_gdf(gdf, crs):
    """
    Fills an empty GeoDataFrame with default values and returns a new GeoDataFrame.

    Parameters:
    gdf (GeoDataFrame): The input GeoDataFrame.
    crs (CRS): The coordinate reference system for the new GeoDataFrame.

    Returns:
    GeoDataFrame: A new GeoDataFrame with default values if the input GeoDataFrame is empty,
    otherwise returns the input GeoDataFrame as is.
    """
    if gdf.empty:
        data = {"infr_length": [0], "geom_length": [0]}
        gdf_new = gpd.GeoDataFrame(
            data,
            columns=[
                "empty",
                "version",
                "timestamp",
                "geometry",
                "tags",
                "geom_length",
                "grid_id",
                "infr_length",
            ],
            geometry="geometry",
            crs=crs,
        )
        return gdf_new
    else:
        return gdf


def to_osmnx_graph(gdf_gdf_to_graph):
    """
    Converts a GeoDataFrame to an OSMNX graph.

    Parameters:
    gdf_gdf_to_graph (GeoDataFrame): The input GeoDataFrame containing the geometries.

    Returns:
    G_gip (networkx.MultiDiGraph): The OSMNX graph representation of the input GeoDataFrame.
    """

    ### Create node GeoDataFrame ###
    nodes = []
    for line in gdf_gdf_to_graph["geometry"]:
        for x, y in [
            line.coords[0],
            line.coords[-1],
        ]:  # get coordinates of first and last point
            node_id = hash(
                (x, y)
            )  # Create a unique id for the node (a pseudo osmid, which is also named 'osmid')
            nodes.append({"osmid": node_id, "x": x, "y": y})

    # Nodes to gdf
    df_nodes = pd.DataFrame(nodes).drop_duplicates().set_index("osmid")
    gdf_nodes = gpd.GeoDataFrame(
        df_nodes, geometry=gpd.points_from_xy(df_nodes.x, df_nodes.y)
    )

    # Create a dictionary with all nodes as key and index (osmid) as value from gdf_nodes
    coord_to_index = {
        node.geometry.coords[0]: index for index, node in gdf_nodes.iterrows()
    }

    ### Create edge GeoDataFrame ###
    edges = []
    for line in gdf_gdf_to_graph["geometry"]:
        u = coord_to_index.get(
            line.coords[0]
        )  # get osmid from gdf_nodes dictionary for start point of line
        v = coord_to_index.get(
            line.coords[-1]
        )  # get osmid from gdf_nodes dictionary for end point of line
        key = hash(u)  # just random unique id for hash
        edges.append({"u": u, "v": v, "key": key, "geometry": line})

    # Create a GeoDataFrame from Edges list
    gdf_edges = gpd.GeoDataFrame(edges, geometry="geometry").set_index(
        ["u", "v", "key"]
    )

    # Set the CRS to match the original GeoDataFrame
    gdf_edges.crs = gdf_gdf_to_graph.crs

    # To (directed) graph
    G_gip = osmnx.graph_from_gdfs(gdf_nodes, gdf_edges)

    return G_gip


#### FUNCTIONS INTRINSIC ANALYSIS ####
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


def load_plot(area, URL, querydict, roads=False, ylabel="Value"):
    """
    Plots a chart of the development of the infrastructure classes cycle tracks, cycle lanes, calm traffic ways
    and main roads over time for a given area with OSHOME API.
    Uses matplotlib, requests, dateutil and datetime modules
    Args:
        area (geojson string): Area in GEOJSON format
        querydict (dictionary): Dictionary with the query strings for the OHSOME API for the different infrastructure classes
        URL (string): OSHOME API URL, see https://api.ohsome.org/v1/swagger-ui.html
        roads (bool, optional): Option to plot also main roads as 4th graph/infrastructure class. Defaults to False.
        ylabel (str, optional): Label text of y-axis. Defaults to 'Value'.
    """
    # Get queries from dictionary
    opwizquery_cycle_tracks = querydict["cycle_tracks"]
    opwizquery_cycle_lanes = querydict["cycle_lanes"]
    opwizquery_calm_traffic_ways = querydict["calm_traffic_ways"]
    opwizquery_own = querydict["own"]
    opwizquery_main_roads = querydict["main_roads"]

    # Set current year and month for API query; 
    # subtract 2 months because OSHOME API has a time delay (otherwise may gives back error)
    dt_2_months_delay = datetime.today() - relativedelta(months=2)
    year = dt_2_months_delay.strftime("%Y")
    month = dt_2_months_delay.strftime("%m")

    def inner_fetch(filter_str):

        date = f"2007-10-08/{year}-{month}-01/P3M"  # special query string for OHSOME: from 2007-10-08 to current date with 6 month interval
        # Fetch OHSOME API
        data = {"bpolys": area, "format": "json", "time": date, "filter": filter_str}
        response = requests.post(URL, data=data, timeout=240)

        # Convert result-property into list
        ls = response.json()["result"]

        # Extract time and values from the dictionaries and put into a list
        try:
            time_ls = [item["timestamp"] for item in ls]
        except Exception:
            time_ls = [item["fromTimestamp"] for item in ls]

        value_ls = [item["value"] for item in ls]

        # Extract year and month
        time_ls = [date.split("-") for date in time_ls]
        time_ls = [f"{date[0]}-{date[1]}" for date in time_ls]

        return [time_ls, value_ls]

    # CREATE FIGURE AND CALL INNER_FETCH

    # Fetch data
    time_value_ls_cycle_tracks = inner_fetch(opwizquery_cycle_tracks)
    time_value_ls_cycle_lanes = inner_fetch(opwizquery_cycle_lanes)
    time_value_ls_calm_traffic_ways = inner_fetch(opwizquery_calm_traffic_ways)

    # Create a figure
    fig = go.Figure()

    # Add Lines
    fig.add_trace(
        go.Scatter(
            x=time_value_ls_cycle_tracks[0],
            y=time_value_ls_cycle_tracks[1],
            mode="lines",
            name="Cycle Tracks",
            line=dict(color="#0a007b"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=time_value_ls_cycle_lanes[0],
            y=time_value_ls_cycle_lanes[1],
            mode="lines",
            name="Cycle Lanes",
            line=dict(color="#006bff"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=time_value_ls_calm_traffic_ways[0],
            y=time_value_ls_calm_traffic_ways[1],
            mode="lines",
            name="Calm Traffic Ways",
            line=dict(color="#c63ddb"),
        )
    )

    # If roads and own parameter is set, plot also main roads or own infrastructure
    if roads:
        time_value_ls_main_roads = inner_fetch(opwizquery_main_roads)
        fig.add_trace(
            go.Scatter(
                x=time_value_ls_main_roads[0],
                y=time_value_ls_main_roads[1],
                mode="lines",
                name="Main Roads",
                line=dict(color="grey"),
            )
        )
    if opwizquery_own:
        time_value_ls_own = inner_fetch(opwizquery_own)
        fig.add_trace(
            go.Scatter(
                x=time_value_ls_own[0],
                y=time_value_ls_own[1],
                mode="lines",
                name="Own Infrastructure",
                line=dict(color="green"),
            )
        )

    # Set layout properties
    fig.update_layout(
        autosize=True,
        xaxis=dict(
            title="Time", linewidth=2, gridcolor="lightgrey", zerolinecolor="lightgrey"
        ),
        yaxis=dict(
            title=ylabel,
            showgrid=True,
            gridcolor="lightgrey",
            zerolinecolor="lightgrey",
        ),
        showlegend=True,
        plot_bgcolor="white",
        font=dict(
            family="Arial",
            color="black",
            size=17,
        ),
    )

    # Show figure
    fig.show()


#### FUNCTIONS CONFLATION ####

def calculate_attribute_amount(gdf):
    """
    Calculates the attribute amount for each row in the given GeoDataFrame.

    Parameters:
    gdf (GeoDataFrame): The input GeoDataFrame.

    Returns:
    GeoDataFrame: The updated GeoDataFrame with the 'attribute_amount' column added.
    """

    gdf['attribute_amount'] = gdf.apply(lambda row: len(row.tags), axis=1)
    return gdf


def group_by_edge_id(gdf, crs,edge_id_col='edge_id'):

    """
    Group a GeoDataFrame by unique edge_ids to dissolve segments and get edge-node model again.
    Only LineStrings which have the same edge_id and attributes are grouped together and dissolved.

    Arguments:
        gdf (geodataframe): GeoDataFrame to be grouped
        edge_id_col (str): name of the column with edge ids

    Returns:
        GeoDataFrame (grouped dataframe): New grouped GeoDataFrame
    """

    # Group by edge_id
    groups = gdf.groupby([edge_id_col])

    # Merge geometries and attributes
    merged_rows = []
    for _, group in groups:
        united_lines = unary_union(group.geometry)
        merged_lines = line_merge(united_lines)
        # Assuming all other attributes are the same (all segments w. same edge_id = one edge = same attributes), 
        # take the first row and replace the geometry
        new_row = group.iloc[0].copy()
        new_row.geometry = merged_lines
        merged_rows.append(new_row)

    # Recreate GeoDataFrame and convert MultiLineStrings to LineStrings
    new_gdf = gpd.GeoDataFrame(merged_rows, crs=crs)
    new_gdf.explode(index_parts=False, inplace=True)
    return new_gdf


def calculate_match_proportion(matched_edges, unmatched_edges, length_col, infr_class=False):
    """
    Calculate the match proportion between matched and unmatched edges.

    Parameters:
    matched_edges (DataFrame): DataFrame containing the matched edges.
    unmatched_edges (DataFrame): DataFrame containing the unmatched edges.
    length_col (str): Name of the column representing the length of the edges.
    infr_class (bool, optional): If specified, only consider edges with the given infr_class. Defaults to False.

    Returns:
    float: The match proportion between matched and unmatched edges in % rounded to the nearest integer.
    """

    if infr_class:
        matched_edges = matched_edges.loc[matched_edges['infr_class'] == infr_class]
        unmatched_edges = unmatched_edges.loc[unmatched_edges['infr_class'] == infr_class]
  
    sum_unmatched_matched = matched_edges[length_col].sum() + unmatched_edges[length_col].sum()
    prop = ((sum_unmatched_matched -  unmatched_edges[length_col].sum()) / sum_unmatched_matched) * 100
    return round(prop, 1)
