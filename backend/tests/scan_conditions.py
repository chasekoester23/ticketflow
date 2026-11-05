"""Fictional scan conditions, applied to a drawn ticket. No real documents.

These stand in for what a scanner does to paper. They check how slots get
classified. Real scanners will differ: thresholds remain uncalibrated.
"""
from __future__ import annotations

import random

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps


def clean(image: Image.Image) -> Image.Image:
    return image


def carbon_copy_pink(image: Image.Image) -> Image.Image:
    """A pale pink second copy from a multi-part form."""
    tint = Image.new("RGB", image.size, (250, 215, 225))
    return Image.blend(ImageEnhance.Contrast(image).enhance(0.55), tint, 0.45)


def carbon_copy_yellow(image: Image.Image) -> Image.Image:
    tint = Image.new("RGB", image.size, (250, 240, 190))
    return Image.blend(ImageEnhance.Contrast(image).enhance(0.6), tint, 0.4)


def low_contrast(image: Image.Image) -> Image.Image:
    return ImageEnhance.Contrast(image).enhance(0.45)


def blurred(image: Image.Image) -> Image.Image:
    return image.filter(ImageFilter.GaussianBlur(2.2))


def skewed(image: Image.Image) -> Image.Image:
    return image.rotate(4, expand=False, fillcolor="white")


def upside_down(image: Image.Image) -> Image.Image:
    return image.rotate(180)


def sideways(image: Image.Image) -> Image.Image:
    return image.rotate(90, expand=True).resize(image.size)


def speckled(image: Image.Image) -> Image.Image:
    copy = image.copy()
    rng = random.Random(3)
    pixels = copy.load()
    for _ in range(copy.width * copy.height // 200):
        pixels[rng.randrange(copy.width), rng.randrange(copy.height)] = (90, 90, 90)
    return copy


def dark_scanner_edge(image: Image.Image) -> Image.Image:
    copy = image.copy()
    ImageDraw.Draw(copy).rectangle([0, 0, 18, copy.height], fill=(40, 40, 40))
    return copy


def coffee_stain(image: Image.Image) -> Image.Image:
    copy = image.copy()
    ImageDraw.Draw(copy, "RGBA").ellipse([200, 300, 520, 620], fill=(120, 80, 30, 70))
    return copy


def photo_shadow(image: Image.Image) -> Image.Image:
    """A phone photo: grey cast with a shadow across one side."""
    gradient = Image.linear_gradient("L").rotate(90).resize(image.size)
    shadow = ImageOps.colorize(gradient, black=(150, 150, 150), white=(235, 235, 235))
    return Image.blend(image, shadow, 0.35)


STYLES = {function.__name__: function for function in (
    clean, carbon_copy_pink, carbon_copy_yellow, low_contrast, blurred, skewed, upside_down, sideways, speckled,
    dark_scanner_edge, coffee_stain, photo_shadow,
)}


# Nothing on these, whatever the scanner added.
def blank_white(size=(680, 880)) -> Image.Image:
    return Image.new("RGB", size, "white")


def blank_scanner_noise(size=(680, 880)) -> Image.Image:
    image = Image.new("RGB", size, (252, 252, 252))
    rng = random.Random(9)
    pixels = image.load()
    for _ in range(size[0] * size[1] // 400):
        pixels[rng.randrange(size[0]), rng.randrange(size[1])] = (215, 215, 215)
    return image


def blank_with_edge_shadow(size=(680, 880)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle([0, 0, 12, size[1]], fill=(70, 70, 70))
    return image


def blank_grey_page(size=(680, 880)) -> Image.Image:
    return Image.new("RGB", size, (228, 228, 228))
