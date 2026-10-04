import os
import shutil
import numpy as np
import torch
import cv2
import einops
from typing import List, Tuple

from .default_utils.DBNet_resnet34 import TextDetection as TextDetectionDefault
from .default_utils import imgproc, dbnet_utils, craft_utils
from .common import OfflineDetector
from ..utils import TextBlock, Quadrilateral, det_rearrange_forward

MODEL = None
def det_batch_forward_default(batch: np.ndarray, device: str):
    global MODEL
    if isinstance(batch, list):
        batch = np.array(batch)
    batch = einops.rearrange(batch.astype(np.float32) / 127.5 - 1.0, 'n h w c -> n c h w')
    batch = torch.from_numpy(batch).to(device)
    with torch.no_grad():
        db, mask = MODEL(batch)
        db = db.sigmoid().cpu().numpy()
        mask = mask.cpu().numpy()
    return db, mask

# Second, smaller detection pass (see DefaultDetector._infer).
_SMALL_PASS_SIZE = 1024
_SMALL_PASS_OVERLAP = 0.3
_SMALL_PASS_GROW = 1.10


class DefaultDetector(OfflineDetector):
    _MODEL_MAPPING = {
        'model': {
            'url': 'https://github.com/zyddnys/manga-image-translator/releases/download/beta-0.3/detect-20241225.ckpt',
            'hash': '67ce1c4ed4793860f038c71189ba9630a7756f7683b1ee5afb69ca0687dc502e',
            'file': '.',
        }
    }

    def __init__(self, *args, **kwargs):
        os.makedirs(self.model_dir, exist_ok=True)
        if os.path.exists('detect-20241225.ckpt'):
            shutil.move('detect-20241225.ckpt', self._get_file_path('detect-20241225.ckpt'))
        super().__init__(*args, **kwargs)

    async def _load(self, device: str):
        self.model = TextDetectionDefault()
        sd = torch.load(self._get_file_path('detect-20241225.ckpt'), map_location='cpu')
        self.model.load_state_dict(sd['model'] if 'model' in sd else sd)
        self.model.eval()
        self.device = device
        if device.startswith('cuda') or device == 'mps' or device == 'xpu':
            self.model = self.model.to(self.device)
        global MODEL
        MODEL = self.model

    async def _unload(self):
        del self.model

    async def _infer(self, image: np.ndarray, detect_size: int, text_threshold: float, box_threshold: float,
                     unclip_ratio: float, verbose: bool = False):
        textlines, raw_mask, extra = await self._infer_single(image, detect_size, text_threshold, box_threshold, unclip_ratio, verbose)
        if detect_size <= _SMALL_PASS_SIZE:
            return textlines, raw_mask, extra
        # A large detect_size finds small text but misses big, heavy lettering
        # (moans / exclamations drawn much larger than dialogue): measured on
        # Tensei Shitara vol.1 p.162 and p.167, where 2048 missed them and 1024
        # found them - while 1024 alone loses small text 2048 finds (p.18). Run a
        # second, smaller pass and keep only the text lines the first one lacks.
        small_lines, small_mask, _ = await self._infer_single(image, _SMALL_PASS_SIZE, text_threshold, box_threshold, unclip_ratio, verbose)
        boxes = [cv2.boundingRect(q.pts.astype(np.int32)) for q in textlines]
        added = 0
        for q in small_lines:
            x, y, w, h = cv2.boundingRect(q.pts.astype(np.int32))
            if w * h <= 0:
                continue
            overlapping = []
            for i, (bx, by, bw, bh) in enumerate(boxes):
                iw = min(x + w, bx + bw) - max(x, bx)
                ih = min(y + h, by + bh) - max(y, by)
                if iw > 0 and ih > 0:
                    overlapping.append((i, iw * ih, bw * bh))
            if not any(inter > _SMALL_PASS_OVERLAP * w * h for _, inter, _ in overlapping):
                textlines.append(q)
                boxes.append((x, y, w, h))
                added += 1
            elif len(overlapping) == 1:
                # The large pass found the line but cut it short (e.g. dropped the
                # trailing hearts of a big moan), so OCR rejects the truncated crop.
                # When the small pass box fully contains it and is clearly larger,
                # prefer the small pass quad.
                i, inter, area = overlapping[0]
                bx, by, bw, bh = boxes[i]
                if inter >= 0.8 * area and max(w, h) >= _SMALL_PASS_GROW * max(bw, bh):
                    textlines[i] = q
                    boxes[i] = (x, y, w, h)
                    added += 1
        if added:
            if small_mask.shape != raw_mask.shape:
                small_mask = cv2.resize(small_mask, (raw_mask.shape[1], raw_mask.shape[0]), interpolation=cv2.INTER_LINEAR)
            raw_mask = np.maximum(raw_mask, small_mask)
            self.logger.info(f'Small-size detection pass added {added} text line(s)')
        return textlines, raw_mask, extra

    async def _infer_single(self, image: np.ndarray, detect_size: int, text_threshold: float, box_threshold: float,
                     unclip_ratio: float, verbose: bool = False):

        # TODO: Move det_rearrange_forward to common.py and refactor
        db, mask = det_rearrange_forward(image, det_batch_forward_default, detect_size, 4, device=self.device, verbose=verbose)

        if db is None:
            # rearrangement is not required, fallback to default forward
            img_resized, target_ratio, _, pad_w, pad_h = imgproc.resize_aspect_ratio(cv2.bilateralFilter(image, 17, 80, 80), detect_size, cv2.INTER_LINEAR, mag_ratio = 1)
            img_resized_h, img_resized_w = img_resized.shape[:2]
            ratio_h = ratio_w = 1 / target_ratio
            db, mask = det_batch_forward_default([img_resized], self.device)
        else:
            img_resized_h, img_resized_w = image.shape[:2]
            ratio_w = ratio_h = 1
            pad_h = pad_w = 0
        self.logger.info(f'Detection resolution: {img_resized_w}x{img_resized_h}')

        mask = mask[0, 0, :, :]
        det = dbnet_utils.SegDetectorRepresenter(text_threshold, box_threshold, unclip_ratio=unclip_ratio)
        # boxes, scores = det({'shape': [(img_resized.shape[0], img_resized.shape[1])]}, db)
        boxes, scores = det({'shape':[(img_resized_h, img_resized_w)]}, db)
        boxes, scores = boxes[0], scores[0]
        if boxes.size == 0:
            polys = []
        else:
            idx = boxes.reshape(boxes.shape[0], -1).sum(axis=1) > 0
            polys, _ = boxes[idx], scores[idx]
            polys = polys.astype(np.float64)
            polys = craft_utils.adjustResultCoordinates(polys, ratio_w, ratio_h, ratio_net=1)
            polys = polys.astype(np.int64)

        textlines = [Quadrilateral(pts.astype(int), '', score) for pts, score in zip(polys, scores)]
        textlines = list(filter(lambda q: q.area > 16, textlines))
        mask_resized = cv2.resize(mask, (mask.shape[1] * 2, mask.shape[0] * 2), interpolation=cv2.INTER_LINEAR)
        if pad_h > 0:
            mask_resized = mask_resized[:-pad_h, :]
        elif pad_w > 0:
            mask_resized = mask_resized[:, :-pad_w]
        raw_mask = np.clip(mask_resized * 255, 0, 255).astype(np.uint8)

        # if verbose:
        #     img_bbox_raw = np.copy(image)
        #     for txtln in textlines:
        #         cv2.polylines(img_bbox_raw, [txtln.pts], True, color=(255, 0, 0), thickness=2)
        #     cv2.imwrite(f'result/bboxes_unfiltered.png', cv2.cvtColor(img_bbox_raw, cv2.COLOR_RGB2BGR))

        return textlines, raw_mask, None
