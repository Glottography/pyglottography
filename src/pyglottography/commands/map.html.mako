<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/purecss@3.0.0/build/pure-min.css" integrity="sha384-X38yfunGUhNzHpBaEBsWLO+A0HDYOQi8ufWDkZ0k9e0eXz/tH3II7uKZ9msv++Ls" crossorigin="anonymous">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
    <style>
        body, html, .vh {
            height: 100vh;
        }
        title {
            text-align: center;
            width: 100%;
        }
        #table {
            display: block;
            margin-left: 10px;
            margin-right: 10px;
            height: 100%;
            overflow-y: scroll;
        }
        #map {
            height: 100%;
            width: 100%;
        }
    </style>
<title>${map.cldf.name}</title>
<style>
    .leaflet-range-control {
    background-color: #fff;
}
.leaflet-range-control.horizontal {
    height: 26px;
    padding-right: 5px;
}
.leaflet-range-control .leaflet-range-icon {
    display: inline-block;
    float: left;
    width: 26px;
    height: 26px;
    background-image: url('data:image/svg+xml;base64,PHN2ZyBmaWxsPSIjMDAwMDAwIiBoZWlnaHQ9IjI0IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHdpZHRoPSIyNCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4NCiAgICA8cGF0aCBkPSJNMCAwaDI0djI0SDB6IiBmaWxsPSJub25lIi8+DQogICAgPHBhdGggZD0iTTE1IDE3djJoMnYtMmgydi0yaC0ydi0yaC0ydjJoLTJ2Mmgyem01LTE1SDRjLTEuMSAwLTIgLjktMiAydjE2YzAgMS4xLjkgMiAyIDJoMTZjMS4xIDAgMi0uOSAyLTJWNGMwLTEuMS0uOS0yLTItMnpNNSA1aDZ2Mkg1VjV6bTE1IDE1SDRMMjAgNHYxNnoiLz4NCjwvc3ZnPg==');
}
.leaflet-range-control input[type=range] {
    display: block;
    cursor: pointer;
    width: 100%;
    margin: 0px;
}
.leaflet-range-control input[type=range][orient=horizontal] {
    margin-top: 5px;
    width: 150px;
}
</style>
</head>

<body>
<div class="pure-g">
    <div class="pure-u-5-5" style="padding-left: 1em; padding-right: 1em;">
        <p><strong>${map.cldf.name or 'Map'}</strong> ${map.cldf.description or map.id}</p>
        <p>in</p>
        % for ref in map.references:
        <blockquote>
            ${str(ref.source)}
        </blockquote>
        % endfor
        <p>
            The map shows the geographic features from the map as digitized in the Glottography
            dataset.</p>
        <p>The table lists Glottolog languoids associated with these features.</p>
    </div>
</div>
<div class="vh pure-g">
    <div class="vh pure-u-${w}-5">
        <div id='map'></div>
    </div>
    <div class="vh pure-u-${5 - w}-5">
        <table id="table" class="pure-table">
            <thead>
            <tr>
                <th>Glottolog languoid</th>
            </tr>
            </thead>
            <tbody>
            <tr>
                <td>
                    <input type="checkbox" id="toggler" onclick="toggle()" />&nbsp;show feature labels
                </td>
            </tr>
            % for lang in languages:
            <tr>
                <td>
                    <a onclick="highlight(`${lang.id}`)" href="#">${lang.cldf.name}</a>
                    <a title="${lang.cldf.name} in Glottolog" href="https://glottolog.org/resource/languoid/id/${lang.id}">[Glottolog]</a>
                </td>
            </tr>
            % endfor
            </tbody>
        </table>
    </div>
</div>
<script>
    var polygons,
        id,
        styles = {
        'regular': {
            'color': '#0000ff',
            'weight': 2,
            'opacity': 0.1},
        'highlight': {
            'color': '#ff0000',
            'weight': 2,
            'opacity': 0.7
        }},
        langlayers = {},
        all_layers = [];

    function onEachFeature(feature, layer) {
        layer.bindTooltip(feature.properties.name);
        layer.setStyle(styles.regular);
        if (!(feature.properties['cldf:languageReference'] in langlayers)) {
            langlayers[feature.properties['cldf:languageReference']] = [];
        }
        langlayers[feature.properties['cldf:languageReference']].push(layer);
        all_layers.push(layer);
    }
    const langs = ${geojson};
	const map = L.map('map').setView([37.8, -96], 4);
	const latLngBounds = L.latLngBounds([[${lat1}, ${lon1}],[${lat2}, ${lon2}]]);

    function highlight(lid) {
        var l;
        for (id in langlayers) {
            for (var i = 0; i < langlayers[id].length; i++) {
                l = langlayers[id][i];
                if (lid === undefined || id === lid) {
                    l.setStyle(styles.highlight);
                    l.openTooltip();
                    map.panTo(l.getBounds().getCenter());
                } else {
                    l.setStyle(styles.regular);
                    l.closeTooltip();
                }
            }
        }
    }

    L.tileLayer(
        'http://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        {
            maxZoom: 18,
            attribution:'&copy; <a href="http://openstreetmap.org/copyright">OpenStreetMap</a> contributors'}
    ).addTo(map);
% if img:
    const imageOverlay = L.imageOverlay('${img}', latLngBounds, {
        opacity: 0.5,
        interactive: true
    }).addTo(map);
% endif
    if (langs) {
        polygons = L.geoJSON(langs, {onEachFeature: onEachFeature}).addTo(map);
    }
    L.control.layers(
        [],
        {
% if img:
            '${map.cldf.name}': imageOverlay,
% endif
            'GeoJSON': polygons},
        {collapsed: false}).addTo(map);
	map.fitBounds(latLngBounds);

    function toggle() {
        var checked = document.getElementById('toggler').checked;
        polygons.eachLayer(function (layer) {
            if (checked) {
                layer.openTooltip();
            } else {
                layer.closeTooltip();
            }
        });
    }


% if img:
    L.Control.Range = L.Control.extend({
        options: {
            position: 'topright',
            min: 0,
            max: 100,
            value: 0,
            step: 1,
            orient: 'vertical',
            iconClass: 'leaflet-range-icon',
            icon: true
        },
    onAdd: function(map) {
        var container = L.DomUtil.create('div', 'leaflet-range-control leaflet-bar ' + this.options.orient);
        if (this.options.icon) {
          L.DomUtil.create('span', this.options.iconClass, container);
        };
        var slider = L.DomUtil.create('input', '', container);
        slider.type = 'range';
        slider.setAttribute('orient', this.options.orient);
        slider.min = this.options.min;
        slider.max = this.options.max;
        slider.step = this.options.step;
        slider.value = this.options.value;

        L.DomEvent.on(slider, 'mousedown mouseup click touchstart', L.DomEvent.stopPropagation);

        L.DomEvent.on(slider, 'mouseenter', function(e) {
            map.dragging.disable()
        });
        L.DomEvent.on(slider, 'mouseleave', function(e) {
            map.dragging.enable();
        });

        L.DomEvent.on(slider, 'change', function(e) {
            this.fire('change', {value: e.target.value});
        }.bind(this));

        L.DomEvent.on(slider, 'input', function(e) {
            this.fire('input', {value: e.target.value});
        }.bind(this));

        this._slider = slider;
        this._container = container;

        return this._container;
    },
    setValue: function(value) {
        this.options.value = value;
        this._slider.value = value;
    },

});

L.Control.Range.include(L.Evented.prototype)

L.control.range = function (options) {
  return new L.Control.Range(options);
};

   var slider = L.control.range({
    position: 'topright',
    min: 0,
    max: 100,
    value: 50,
    step: 1,
    orient: 'horizontal',
    iconClass: 'leaflet-range-icon',
    icon: true
});

slider.on('input change', function(e) {
   imageOverlay.setOpacity(e.value / 100);
});

    map.addControl(slider);
% endif
</script>
</body>
</html>
