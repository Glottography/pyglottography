# Changes


## [1.1.0] - 2025-10-12

- Fixed bug whereby invalid geometries might have been created for families due to
  issues introduced by simplification.
- Changed the data model of Glottography CLDF datasets in backwards incompatible
  ways.
- Added command to create an HTML page displaying one map of a dataset, mostly for
  quality control.
- `makecldf` will now add shapes available for dialects as speaker areas as well.


## [1.0.0] - 2025-04-07

Fine-tuned repair mechanisms.


## [0.2.0] - 2025-02-10

- Added two mechanisms to fix shape geometries based on config data.
- Don't use cldfgeojson.dump since cutting off decimal places from coordinates may introduce
  invalid geometries.


## [0.1.0] - 2024-11-22

pyglottography provides
- a cldfbench project template to bootstrap new Glottography datasets,
- a `cldfbench.Dataset` subclass, implementing the Glottography publication workflow.

