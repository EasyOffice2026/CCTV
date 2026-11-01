# AI models: which ones we may use, and what we recommend (plan 0.7, §1 #12)

The AI box needs trained models to see people, kitchen hygiene items, the cash drawer and
staff faces. The software for all of them is built; **the models aren't installed yet**, and
until they are, the box only detects motion. This page compares the options by **licence**
(may we use it in a product we sell?), accuracy and speed, and recommends a choice.

Licences checked on 2 November 2026 from each project's official page (sources below).
Licences change: check again before buying or shipping, and have a lawyer confirm the
shortlist (plan 0.5).

## The decision in short

| Job | Recommended | Licence | Cost | Ready in our software |
|---|---|---|---|---|
| **People** (security after hours, restricted areas, counting, queues, heatmap, unattended counter, kitchen crops) | **D-FINE-S**, COCO-only checkpoint (D-FINE-M if more accuracy is needed) | Apache-2.0: commercial use allowed | Free | Yes (`rtdetr` format) |
| People, alternative | RF-DETR Small or Medium | Apache-2.0 (only Nano to Large; not the "Plus" XL/2XL) | Free | Yes (`rtdetr` format) |
| People, fallback | YOLOX-S | Apache-2.0 | Free | Yes (`yolox` format) |
| **Staff faces** (attendance) | **YuNet** (face finder) + **SFace** (face matcher), OpenCV model zoo | MIT and Apache-2.0 | Free | Yes (already wired) |
| Staff faces, alternative | Hikvision face-recognition terminal at the staff door (plan 5.4, option A) | Comes with the device | Hardware price | Needs a small connector |
| **Mask / gloves / hair cap**, **cash drawer open** | **Our own small models, trained on Mudawara's footage** | Ours | Labelling time | Yes (classifier formats) |

**Recommended decision:** approve D-FINE (COCO-only), YuNet + SFace, and training our own
hygiene and drawer models. All are free, all allow commercial use, and all already plug into
the box. Nothing has to be bought.

## Not recommended

| Model | Why not |
|---|---|
| **Ultralytics YOLO** (YOLOv5, v8, 11 and later) | AGPL-3.0: a product using it must publish its whole source code to its users, or buy Ultralytics' Enterprise licence (price on request, per company). Fine technically, but costs money or our code |
| YOLOv7, YOLOv9 (original authors' code) | GPL-3.0: the same kind of obligation to publish our code |
| YOLO-NAS | The code is Apache-2.0 but the trained weights are under Deci's licence, "research and non-commercial use only"; the project is no longer maintained |
| D-FINE checkpoints pretrained on Objects365 (`obj365`, `obj2coco`) | The D-FINE project says these aren't cleared for commercial use (Objects365's terms). Use the COCO-only checkpoints |
| RF-DETR XL / 2XL ("Plus") | A separate, paid licence (PML 1.0) |
| InsightFace face models (`buffalo_l` and others) | Non-commercial research only; a commercial licence must be requested from InsightFace |
| NVIDIA PeopleNet (TAO) | Under NVIDIA's model licence; whether it may be resold inside our product isn't clear. Ask NVIDIA in writing if ever wanted |

Our model helper refuses the checkpoints in this table that it can recognise by name
(`python -m cctv_edge.models.make_manifest`, below), and the box refuses any model whose
`model.json` doesn't state a licence.

## Accuracy and speed

People detectors, accuracy on the standard COCO test (higher is better) and speed on a server
graphics card (T4, TensorRT), from the projects' own tables:

| Model | COCO AP | Time per image (T4) |
|---|---|---|
| D-FINE-N | 42.8 | 2.1 ms |
| **D-FINE-S** | **48.5** | **3.5 ms** |
| D-FINE-M | 52.3 | 5.6 ms |
| D-FINE-L | 54.0 | 8.1 ms |
| YOLOX-S | 40.5 | not published for a T4 |

RF-DETR's Apache-2.0 models are in the same range or better according to Roboflow's
benchmark; compare them on our footage if D-FINE falls short.

On the Jetson box: 8 cameras analysed 5 times a second is 40 images a second, so each image
must take under 25 ms. A Jetson Orin is several times slower than a T4, so D-FINE-S should fit
comfortably, and D-FINE-M probably. **This has to be measured on the real box** (plan 2.2:
video decoding is still on the processor, which takes its own share).

What matters most is accuracy **on Mudawara's cameras**: ceiling angles, kitchen light,
reflections. Whatever we choose is checked on the labelled footage (the 90% target, plan 5.5)
and improved by training on it.

## Trial on a development PC (2 November 2026)

D-FINE-S (COCO-only, the ready-made ONNX export on Hugging Face,
`onnx-community/dfine_s_coco-ONNX`, from `ustc-community/dfine-small-coco`, Apache-2.0) run
through the box's own code (`model.json` from `make_manifest --preset dfine-hf`, the box's
loader with its sha256 and licence checks, its detector):

| Test | Result |
|---|---|
| A restaurant kitchen photo (COCO) | The cook found (95 %), and a hand at the edge of the picture (75 %) |
| A street photo with three people | All three (95–96 %) |
| A photo of cats | Nobody (correct) |
| 10 s of video at 02:00, branch open 10:00–23:00, through `replay` (decoding, detection, tracking, rules) | One "Person after hours" event over the 10 s (96 %) |
| Time per image on the PC's processor (no graphics card) | About 120 ms |

So the model works with the box as built. On a processor alone it would handle only about
one camera; the box uses the Jetson's graphics chip (TensorRT), which must be measured on the
real box. Accuracy on Mudawara's cameras is still to be measured on their footage.

## One thing for the lawyer

The free models were trained on public photo collections (COCO's photos come from Flickr under
Creative Commons licences). Selling products that use models trained on such data is common
practice, but the law on it isn't settled everywhere. Our own models are trained on
Mudawara's footage under the client's agreement. Ask counsel to confirm both points along
with the consent forms (plan 0.5).

## After you decide

1. Get the model as ONNX: for D-FINE, the ready-made export on Hugging Face
   (`onnx-community/dfine_s_coco-ONNX`, `onnx/model.onnx`; preset `dfine-hf`), or a COCO-only
   `.pth` from the project's releases exported with `tools/deployment/export_onnx.py` (preset
   `dfine`). RF-DETR: `model.export()`; YOLOX: `tools/export_onnx.py`.
2. Check the export's licence and that it is the COCO-only checkpoint.
3. Put it in a folder as `model.onnx` and write its `model.json`:

   ```bash
   python -m cctv_edge.models.make_manifest models/person-dfine-s --preset dfine \
       --name person-dfine-s --version 2026-11
   ```

   The presets know each family's input and class numbering (D-FINE and RF-DETR use COCO's
   original category ids, YOLOX counts 0–79), and the result is loaded the way the box loads
   it before it is written.
4. Run it over recorded footage (`python -m cctv_edge.replay`) and measure accuracy
   (`python -m cctv_ml.evaluate`) against the labelled test set (ml/README.md).
5. Copy it onto the box with `MODELS_SOURCE=... provision.sh`; then shadow mode on site (plan 9.4).

The face models (YuNet, SFace) go the same way, with their own `model.json` (edge/README.md).
The hygiene and drawer models need the footage from the site visit, labelled (plan 0.4, 5.0).

## Sources

- Ultralytics licensing: https://ultralytics.com/license
- D-FINE (Apache-2.0; Objects365 note; model table): https://github.com/Peterande/D-FINE
- RF-DETR (Apache-2.0 and PML 1.0 tiers): https://github.com/roboflow/rf-detr
- YOLOX (Apache-2.0): https://github.com/Megvii-BaseDetection/YOLOX
- YOLO-NAS weights licence: https://roboflow.com/model-licenses/yolo-nas
- InsightFace licence: https://pypi.org/project/insightface/
- NVIDIA TAO licences: https://forums.developer.nvidia.com/t/deepstream-tao-license/333074
- YuNet (MIT): https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
- SFace (Apache-2.0): https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface
