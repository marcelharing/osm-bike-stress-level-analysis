# OpenStreetMap Bike Stress Level Analysis

**This is a brief follow-up to the [OSM Bike Analysis project](https://github.com/marcelharing/osm-bike-analysis). With this I want to get a more qualitative overview of the bicycle connections in a specified area. More specifically, the bikeability of the traffic infrastructure is calculated.**

The issue with the classification of bicycle-only infrastructure, as seen in the OSM Bike Analysis, is that it often doesn't reflect the real situation. This may be because people are not using it because it is not comfortable enough, or because it is not the fastest connection. It may also be because a broad and coarse classification does not reflect properties such as smoothness, usage density or steepness. A more qualitative, yet still data-driven approach can be used to assess these issues: Multifactor analysis of stress level classification on every possible traffic infrastructure is a good solution for this. I will use a Jupyter Notebook (with necessary geodate packages) for this and take into account four attributes (tags) from OpenStreetMap features.

## Method

OSM provides some quite good attributes for features representing the traffic infrastructure. These features with attributes can be collected and called with Overpass Turbo. I will focus on these four attributes:
* Surface of ways
* Road classification (e.g. cycle tracks, residential streets)
* Maximum speed
* Smoothness
  
They are not too vague and are quite meaningful and popular among OSM editors. The problem arises if some attributes are missing (surface, maximum speed and smoothness are only optional attributes in OSM). In that case, I have to rely on default values. However, this is also problematic because missing tags could misrepresent the current situation on the ground. For a more advanced approach to deal with default values, see, for example: https://github.com/SupaplexOSM/OSM-Cycling-Quality-Index.

Next, each of the four attributes is given a score. Each attribute type can gain a maximum of 10 points, so the total possible score is 40. I take a very simple heuristic approach, assigning fixed points to each value based on how important this property is for using it comfortably might be. The advantage of this approach is, that it represents quite good the feeling of using infrastructure with this certain property. On the other hand, this score may be somewhat subjective.

| category | tag | value (1–10) |
|---|---|---:|
| Smoothness | very_horrible | 1 |
| Smoothness | horrible | 2 |
| Smoothness | very_bad | 3 |
| Smoothness | bad | 4 |
| Smoothness | intermediate | 6 |
| Smoothness | good | 8 |
| Smoothness | excellent | 10 |
| Surface | unhewn_cobblestone | 1 |
| Surface | grass | 2 | 
| Surface | dirt | 2 |
| Surface | earth | 2 | 
| Surface | cobblestone | 2 |
| Surface | unpaved | 3 |
| Surface | ground | 3 |
| Surface | gravel | 4 |
| Surface | sett | 4 | 
| Surface | paving_stones | 4 |
| Surface | compacted | 5 |
| Surface | grass_paver | 6 |
| Surface | fine_gravel | 6 |
| Surface | paved | 8 |
| Surface | concrete | 10 |
| Surface | asphalt | 10 |
| Maxspeed | >50 | 1 |
| Maxspeed | 50 | 2 |
| Maxspeed | 40 | 4 |
| Maxspeed | 30 | 6 |
| Maxspeed | 20 | 8 |
| Maxspeed | 10 | 10 |
| Highway class | primary/primary_link | 1 |
| Highway class | secondary/secondary_link | 2 |
| Highway class | tertiary/tertiary_link | 3 |
| Highway class | unclassified | 5 |
| Highway class | track | 7 |
| Highway class | residential | 7 |
| Highway class | pedestrian | 8 |
| Highway class | living_street | 9 |
| Highway class | path | 9 |
| Highway class | footway | 10 |
| Highway class | cycleway | 10 |

Finally, I am calculating four stress level categories with equal intervals.

<img src="https://i.imgur.com/YQS8MyY.png" alt="Stress Level Score Class" width="430"/>

## Results
<img src="https://i.imgur.com/lykiXTy.png" alt="Stress Level Score Class" width="530"/>

The final classification indicates where cycling is most comfortable: on smooth cycleways and calm residential streets. However, as can be seen on the output map, missing attributes are a real problem. In this example, there are many service road features that often lack surface, smoothness and maximum speed values. Setting default values for these features is unlikely to accurately represent the situation because service roads vary considerably in quality (somtimes they are paved, sometimes not etc.). Another issue is the very fine-grained but complex access attributes in OSM; at the end you can't always tell which are usable by bikes.

So using four attributes to assess bikeability is a good approach for an initial overview. To achieve better results, a more sophisticated default value scoring system could be implemented. In addition, missing values could be added to OSM systematically to create meaningfull bikeabilty maps for cities. Surface status can added often quite easily with optical satellite images; maxspeed and smothness with street imagery. 

For more detailed studies on bikeability or stress level classification, see:
* Wysling, Laura/Purves, Ross S. (2022). Where to improve cycling infrastructure? Assessing bicycle suitability and bikeability with open data in the city of Paris. Transportation Research Interdisciplinary Perspectives 15, 100648.
https://doi.org/10.1016/j.trip.2022.100648.
* Schmid-Querg, Jonas/Keler, Andreas/Grigoropoulos, Georgios (2021). The Munich. Bikeability Index: A Practical Approach for Measuring Urban Bikeability. Sustainability 13 (1), 428. https://doi.org/10.3390/su13010428. Schmid-Querg et al.
* Hardinghaus, Michael/Weschke, Jan (2023). Transforming bicycle market: Assessing cyclists route preferences on different bike types in a choice experiment. Transportation Research Interdisciplinary Perspectives 22, 100921.
https://doi.org/10.1016/j.trip.2023.100921.
* Abad, Lorena/van der Meer, Lucas (2018). Quantifying Bicycle Network Connectivity in Lisbon Using Open Data. Information 9 (11), 287. https://doi.org/10.3390/info9110287.
