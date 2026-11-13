`tiled-colors.heic` is a synthetic 480 × 352 image made from four independently encoded 256 × 192 HEVC tiles: red, green, blue and yellow. Its HEIF grid crops a 512 × 384 canvas. It contains no personal photos or metadata.

The fixture reproduces FFmpeg selecting one tile when given `-map 0:v:0`. Tests check all four colors and the cropped dimensions through the fallback decoder.

`tiled-colors-rotated.heic` has the same generated tiles with an HEIF `irot` property rotating the complete image 90° counterclockwise.

`tiled-colors-multiple-rotations.heic` rotates the primary grid 90° counterclockwise while its tiles reference a separate zero-rotation property. An unqualified ExifTool `Rotation` reads zero, reproducing selection of another item's direction. All fixtures are synthetic.
