# SC4MP API reference

The SC4MP API scans the SC4MP network for servers and serves what it finds as JSON, along with region maps and city images. The public instance is at `https://api.sc4mp.org`.

All endpoints are `GET`, need no authentication, and allow requests from any origin (CORS). Unknown servers, regions and cities return `404`.

The scanner starts from `servers.sc4mp.org` and follows each server's own server list. It fetches every server it finds about once a minute. A server that hasn't responded for 30 days is dropped.

- [Servers](#servers)
  - [`GET /servers`](#get-servers)
  - [`GET /servers/<server_id>`](#get-serversserver_id)
- [Regions](#regions)
  - [`GET /servers/<server_id>/regions`](#get-serversserver_idregions)
  - [`GET /servers/<server_id>/regions/<region>/map.png`](#get-serversserver_idregionsregionmappng)
  - [`GET /servers/<server_id>/regions/<region>/map.json`](#get-serversserver_idregionsregionmapjson)
- [Cities](#cities)
  - [`GET /cities`](#get-cities)
  - [`GET /cities/<checksum>`](#get-citieschecksum)
  - [`GET /cities/<checksum>/image.png`](#get-citieschecksumimagepng)
- [Running the API](#running-the-api)


## Servers

### `GET /servers`

Every server the scanner has found, as a list of [server objects](#server-object).

### `GET /servers/<server_id>`

One server, as a [server object](#server-object).

### Server object

```json
{
  "server_id": "PvRLyX5QFkSXkHqv5i2HRDTqithhK5xC",
  "host": "127.0.0.1",
  "port": 7240,
  "url": "sc4mp://127.0.0.1:7240",
  "version": "0.8.5",
  "updated": "2026-10-22 02:38:06",
  "info": {
    "server_id": "PvRLyX5QFkSXkHqv5i2HRDTqithhK5xC",
    "server_name": "My Server",
    "server_description": "Join and build your city.",
    "server_url": "www.sc4mp.org",
    "server_version": "0.8.5",
    "private": false,
    "password_enabled": false,
    "user_plugins_enabled": false,
    "claim_duration": 30,
    "max_region_claims": 1,
    "godmode_filter": true
  },
  "stats": {
    "stat_mayors": 0,
    "stat_mayors_online": 0,
    "stat_claimed": 0.0,
    "stat_download": 59587641
  }
}
```

| Field | Description |
|---|---|
| `server_id` | The server's ID. |
| `host`, `port` | Where the server is. |
| `url` | The server's `sc4mp://` link, which opens it in the SC4MP Launcher. |
| `version` | The SC4MP Server version it runs. |
| `updated` | When the scanner last fetched it, in UTC (`YYYY-MM-DD HH:MM:SS`). |
| `info` | The server's info, as the server sends it. Fields depend on the server's version (for example `claim_duration`, `max_region_claims` and `godmode_filter` are missing on older servers, and newer ones may add more). `claim_duration` and `max_region_claims` are `null` for none. |
| `stats` | Only for servers that aren't private. |
| `stats.stat_mayors` | How many mayors have claimed a city. |
| `stats.stat_mayors_online` | How many players are online. Servers with chat report the players in game on the server (`info.players_online`); for other servers, it's how many mayors saved a city in the last hour. |
| `stats.stat_claimed` | The share of the regions' area that's claimed, from `0` to `1`. |
| `stats.stat_download` | The size of the server's plugins and regions, in bytes. |


## Regions

Region maps are put together from the image each city's savegame has for SC4's region view, placed like the region view. Only public servers have regions. The scanner downloads up to 100 MB of new savegames per server per scan, so large servers fill in over several scans.

### `GET /servers/<server_id>/regions`

The server's regions, by name.

```json
{
  "London": {
    "cities": 73,
    "ready": 73,
    "map": "/servers/PvRLyX5QFkSXkHqv5i2HRDTqithhK5xC/regions/London/map.png",
    "map_cities": "/servers/PvRLyX5QFkSXkHqv5i2HRDTqithhK5xC/regions/London/map.json"
  }
}
```

| Field | Description |
|---|---|
| `cities` | How many cities (savegames) the region has. |
| `ready` | How many of them are on the map so far. A region with `0` has no map yet. |
| `map` | The region map's path. |
| `map_cities` | The path of the cities on the map. |

### `GET /servers/<server_id>/regions/<region>/map.png`

The region map, as a PNG with a transparent background.

| Parameter | Description |
|---|---|
| `width` | Optional, `1600` by default. The map is drawn at the first of `400`, `800`, `1600` or `4096` px that's at least this wide (or smaller, if the full-size map is). Must be a whole number, or the request returns `400`. |

Maps are cached until a city in the region changes, and can be cached by browsers for 5 minutes. A map that isn't cached yet can take a few seconds to draw. Returns `503` if the API runs without Pillow.

### `GET /servers/<server_id>/regions/<region>/map.json`

Where each city is on the map, with its stats from the server's region database, for showing city info over the map.

```json
{
  "width": 2046.5,
  "height": 1030.5,
  "cities": [
    {
      "checksum": "4ff996229f378fd7ddde0019ae7453dd",
      "city": "/cities/4ff996229f378fd7ddde0019ae7453dd",
      "x": 14,
      "y": 14,
      "size": 2,
      "outline": [[0.61911, 0.93984], [0.65551, 0.8525], [0.74395, 0.88695], [0.74395, 0.90733], [0.70755, 0.99466], [0.61911, 0.96021]],
      "city_name": "Fulham",
      "mayor_name": "Robert",
      "claimed": false,
      "population": 4526,
      "residential_population": 0,
      "commercial_population": 0,
      "industrial_population": 4526,
      "mayor_rating": 0,
      "difficulty": 0,
      "total_funds": 21148,
      "modified": null
    }
  ]
}
```

| Field | Description |
|---|---|
| `width`, `height` | The full-size map's size, in pixels. |
| `cities` | The cities, front to back, so the first city whose outline contains a point is the one on top there. |
| `checksum` | The MD5 of the city's savegame. |
| `city` | The city's path under [`/cities`](#get-citieschecksum). |
| `x`, `y`, `size` | The city's tile in the region, and its size in tiles (`1` small, `2` medium, `4` large). |
| `outline` | The city's outline on the map, as points from `0` to `1` of the map's width and height, so it fits the map at any width. |
| `claimed` | Whether a mayor owns the city. |
| `modified` | When it was last saved to the server, in the server's time (`YYYY-MM-DD HH:MM:SS`), or `null`. |
| other fields | The city's stats from the server's region database, as in [city objects](#city-object). |


## Cities

Cities are kept by the MD5 of their savegame, so a city is only read again when its savegame changes. Only the cities of public servers are known.

### `GET /cities`

Every city on the servers the scanner has found, as a list of [city objects](#city-object).

### `GET /cities/<checksum>`

One city, as a [city object](#city-object). `<checksum>` is the MD5 of its savegame, in lowercase hex.

### City object

```json
{
  "checksum": "34fa6ca62a0f645c3cfe508b2f203433",
  "city_name": "Claudeville",
  "mayor_name": "Claude",
  "x": 8,
  "y": 12,
  "size": 4,
  "population": 2273,
  "residential_population": 1451,
  "commercial_population": 277,
  "industrial_population": 545,
  "mayor_rating": 42,
  "difficulty": 0,
  "gamemode": 1,
  "total_funds": 370584,
  "image": "/cities/34fa6ca62a0f645c3cfe508b2f203433/image.png",
  "locations": [
    {"server_id": "PvRLyX5QFkSXkHqv5i2HRDTqithhK5xC", "region": "Hills"}
  ]
}
```

| Field | Description |
|---|---|
| `checksum` | The MD5 of the city's savegame. |
| `city_name`, `mayor_name` | As saved in the city. |
| `x`, `y`, `size` | The city's tile in its region, and its size in tiles (`1` small, `2` medium, `4` large). |
| `population` | Residential, commercial and industrial population added up. |
| `mayor_rating` | From `0` to `100`. |
| `difficulty` | The difficulty the city was started on, as SC4 saves it. |
| `gamemode` | `0` for a city that hasn't been established (mayor mode not started yet), `1` otherwise. |
| `total_funds` | The city's funds, in simoleons. |
| `image` | The path of the city's image. |
| `locations` | The servers and regions the savegame is in. |

Cities read before the API kept their stats have `null` for them.

### `GET /cities/<checksum>/image.png`

The city's image, as SC4 shows it in the region view, as a PNG with a transparent background. Its width is 128 px per tile plus 2. Since it's kept by the savegame's MD5, it never changes, and browsers can cache it for a day.


## Running the API

```
python sc4mpapi.py [--host HOST] [--port PORT]
```

It needs Python 3.8, Flask and PySocks, and Pillow for the region maps (`pip install -r requirements.txt`). Without Flask, only the scanner runs.

It keeps its data in `_SC4MP/_Database`:

| Folder | Contents |
|---|---|
| `servers` | Each server found, served again right after a restart. |
| `regions` | Each server's regions, for its maps after a restart. |
| `cities` | Each savegame's image and stats, by MD5. |
| `maps` | Region maps already drawn. |

Servers that have expired, and cities and maps unused for 30 days, are deleted at the end of each scan. The log is written to `sc4mpapi.log`.
