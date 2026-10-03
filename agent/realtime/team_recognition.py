"""团队页面识别。模板由真实录像裁切，状态识别不依赖用户名或歌曲。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .vision_io import imread_unicode

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / 'resource/image/team'
DIFFICULTY_TARGETS = {
    'Easy': (602, 575), 'Normal': (687, 575),
    'Hard': (769, 575), 'Expert': (852, 575),
}
SONG_LEVEL_ROI = (142, 580, 47, 34)
SONG_TITLE_ROI = (106, 536, 440, 70)


@dataclass(frozen=True)
class TeamScreen:
    state: str
    button: tuple[int, int] | None = None
    loading_progress: tuple[int, ...] | None = None


def loading_progress(image):
    """Approximate ten bar fills; call only on an identified loading page.

    Read the gray unfilled suffix, ignoring stickers over the filled section.
    Low-confidence frames cannot keep a room alive. These values are for
    progress detection, never for deciding whether Native should start.
    """
    values = []
    for x in (146, 670):
        for y in (237, 314, 391, 468, 545):
            bar = image[y-3:y+4, x:x+462].astype(np.int16)
            gray = ((bar.max(2)-bar.min(2) < 15)
                    & (bar.mean(2) > 65) & (bar.mean(2) < 140))
            pink = ((bar[:,:,2] > 180) & (bar[:,:,2]-bar[:,:,1] > 40)
                    & (bar[:,:,2]-bar[:,:,0] > 30))
            if np.mean(gray | pink) < .70:
                return None
            suffix = 0
            for unfilled in (np.mean(gray, axis=0) > .6)[::-1]:
                if not unfilled:
                    break
                suffix += 1
            if not suffix and not np.mean(pink[:,-5:]) > .6:
                return None
            values.append(round(100*(1-suffix/462)))
    return tuple(values)


class TeamRecognizer:
    def __init__(self, directory: Path = TEMPLATE_DIR):
        self.specs = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))['templates']
        self.templates = {k: imread_unicode(directory / v['file']) for k, v in self.specs.items()}
        if any(im is None or im.size == 0 for im in self.templates.values()):
            raise ValueError('团队演出识别模板缺失或损坏')

    def box(self, image, name: str) -> tuple[int, int, int, int] | None:
        if not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3):
            return None
        spec, template = self.specs[name], self.templates[name]
        x, y, w, h = spec.get('search_roi', spec['roi'])
        left, top = max(0, x-6), max(0, y-6)
        crop = image[top:min(720, y+h+6), left:min(1280, x+w+6)]
        th, tw = template.shape[:2]
        if crop.shape[0] < th or crop.shape[1] < tw:
            return None
        _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED))
        if score < spec['threshold']:
            return None
        hit = crop[loc[1]:loc[1]+th, loc[0]:loc[0]+tw]
        # Correlation alone also matches buttons behind a dark modal overlay.
        if float(np.abs(hit.astype(np.float32)-template.astype(np.float32)).mean()) > 24:
            return None
        return left+loc[0], top+loc[1], tw, th

    def point(self, image, name: str) -> tuple[int, int] | None:
        box = self.box(image, name)
        return None if box is None else (box[0]+box[2]//2, box[1]+box[3]//2)

    def network_modal(self, image) -> TeamScreen | None:
        if self.box(image, 'network_unavailable'):
            return TeamScreen('network_unavailable', self.point(image, 'network_ok'))
        if self.box(image, 'connect_error'):
            return TeamScreen('connect_error', self.point(image, 'retry'))
        return None

    def observe(self, image, *, results: bool = False) -> TeamScreen:
        # Modal identities take priority; never click through a dialog.
        modal = self.network_modal(image)
        if modal is not None:
            return modal
        if results:
            for name, button in (
                ('activity_reward', 'activity_reward_ok'),
                ('reward_popup', 'reward_ok'), ('achievement', 'achievement_close'),
                ('activity', 'confirm_result'), ('experience', 'next_detail'),
                ('pggbm', 'next_detail'), ('team_result', 'next'),
            ):
                if self.box(image, name):
                    return TeamScreen(name, self.point(image, button))
            return TeamScreen('unknown')
        if self.box(image, 'prepare'):
            if self.box(image, 'cancel_ready'):
                return TeamScreen('ready_wait')
            return TeamScreen('prepare', self.point(image, 'ready'))
        for name in ('loading', 'random_song', 'matching', 'full', 'lobby', 'home', 'entry'):
            if self.box(image, name):
                # No lobby/full/start button action: public rooms advance automatically.
                button = self.point(image, 'home_ok') if name == 'home' else None
                return TeamScreen(name, button,
                                  loading_progress(image) if name == 'loading' else None)
        return TeamScreen('unknown')
