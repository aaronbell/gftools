"""
Functions to fix fonts so they conform to the Google Fonts
specification:
https://github.com/googlefonts/gf-docs/tree/main/Spec
"""
from fontTools.misc.fixedTools import otRound
from fontTools.ttLib import TTFont, newTable, getTableModule
from fontTools.ttLib.tables import ttProgram
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from fontTools.ttLib.tables._f_v_a_r import NamedInstance
from gftools.util.google_fonts import _KNOWN_WEIGHTS
from gftools.utils import (
    download_family_from_Google_Fonts,
    Google_Fonts_has_family,
    font_stylename,
    font_familyname,
    family_bounding_box,
    get_unencoded_glyphs,
    normalize_unicode_marks,
    partition_cmap,
    typo_metrics_enabled,
    validate_family,
    unique_name,
)
from axisregistry import (
    build_filename,
    build_name_table,
    build_fvar_instances,
    build_variations_ps_name
)
from gftools.stat import gen_stat_tables

from os.path import basename, splitext
from copy import deepcopy
import logging
import subprocess
import os
import tempfile


log = logging.getLogger(__name__)


__all__ = [
    "remove_tables",
    "add_dummy_dsig",
    "fix_unhinted_font",
    "fix_hinted_font",
    "fix_fs_type",
    "fix_weight_class",
    "fix_fs_selection",
    "fix_mac_style",
    "fix_fvar_instances",
    "fix_nametable",
    "inherit_vertical_metrics",
    "fix_vertical_metrics",
    "fix_ascii_fontmetadata",
    "drop_nonpid0_cmap",
    "drop_mac_cmap",
    "fix_pua",
    "fix_isFixedPitch",
    "drop_mac_names",
    "drop_superfluous_mac_names",
    "fix_font",
    "fix_family",
    "rename_font",
    "fix_filename"
]


# The _KNOWN_WEIGHT_VALUES constant is used internally by the GF Engineering
# team so we cannot update ourselves. TODO (Marc F) unify this one day
WEIGHT_NAMES = _KNOWN_WEIGHTS
del WEIGHT_NAMES[""]
WEIGHT_NAMES["Hairline"] = 1
WEIGHT_NAMES["ExtraBlack"] = 1000
WEIGHT_VALUES = {v: k for k, v in WEIGHT_NAMES.items()}


UNWANTED_TABLES = frozenset(
    [
        "FFTM",
        "TTFA",
        "TSI0",
        "TSI1",
        "TSI2",
        "TSI3",
        "TSI5",
        "prop",
        "Debg",
    ]
)


def remove_tables(ttFont, tables=None):
    """Remove unwanted tables from a font. The unwanted tables must belong
    to the UNWANTED_TABLES set.

    Args:
        ttFont: a TTFont instance
        tables: an iterable containing tables remove
    """
    tables_to_remove = UNWANTED_TABLES if not tables else frozenset(tables)
    font_tables = frozenset(ttFont.keys())

    tables_not_in_font = tables_to_remove - font_tables
    if tables_not_in_font:
        log.warning(
            f"Cannot remove tables '{list(tables_not_in_font)}' since they are "
            f"not in the font."
        )

    required_tables = tables_to_remove - UNWANTED_TABLES
    if required_tables:
        log.warning(
            f"Cannot remove tables '{list(required_tables)}' since they are required"
        )

    tables_to_remove = UNWANTED_TABLES & font_tables & tables_to_remove
    if not tables_to_remove:
        return
    log.info(f"Removing tables '{list(tables_to_remove)}' from font")
    for tbl in tables_to_remove:
        del ttFont[tbl]


def add_dummy_dsig(ttFont):
    """Add a dummy dsig table to a font. Older versions of MS Word
    require this table.

    Args:
        ttFont: a TTFont instance
    """
    newDSIG = newTable("DSIG")
    newDSIG.ulVersion = 1
    newDSIG.usFlag = 0
    newDSIG.usNumSigs = 0
    newDSIG.signatureRecords = []
    ttFont.tables["DSIG"] = newDSIG


def fix_unhinted_font(ttFont):
    """Improve the appearance of an unhinted font on Win platforms by:
        - Add a new GASP table with a newtable that has a single
          range which is set to smooth.
        - Add a new prep table which is optimized for unhinted fonts.

    Args:
        ttFont: a TTFont instance
    """
    gasp = newTable("gasp")
    # Set GASP so all sizes are smooth
    gasp.gaspRange = {0xFFFF: 15}

    program = ttProgram.Program()
    assembly = ["PUSHW[]", "511", "SCANCTRL[]", "PUSHB[]", "4", "SCANTYPE[]"]
    program.fromAssembly(assembly)

    prep = newTable("prep")
    prep.program = program

    ttFont["gasp"] = gasp
    ttFont["prep"] = prep


def fix_hinted_font(ttFont):
    """Improve the appearance of a hinted font on Win platforms by enabling
    the head table's flag 3.

    Args:
        ttFont: a TTFont instance
    """
    if not 'fpgm' in ttFont:
        return False, ["Skipping. Font is not hinted."]
    old = ttFont["head"].flags
    ttFont["head"].flags |= 1 << 3
    return ttFont["head"].flags != old


def fix_fs_type(ttFont):
    """Set the OS/2 table's fsType flag to 0 (Installable embedding).

    Args:
        ttFont: a TTFont instance
    """
    old = ttFont["OS/2"].fsType
    ttFont["OS/2"].fsType = 0
    return old != 0


def fix_weight_class(ttFont):
    """Set the OS/2 table's usWeightClass so it conforms to GF's supported
    styles table:
    https://github.com/googlefonts/gf-docs/tree/main/Spec#supported-styles

    Args:
        ttFont: a TTFont instance
    """
    old_weight_class = ttFont["OS/2"].usWeightClass

    if 'fvar' in ttFont:
        fvar = ttFont['fvar']
        default_axis_values = {a.axisTag: a.defaultValue for a in fvar.axes}
        v = default_axis_values.get('wght', None)

        if v is not None:
            ttFont["OS/2"].usWeightClass = int(v)
            return ttFont["OS/2"].usWeightClass != old_weight_class

    stylename = font_stylename(ttFont)
    tokens = stylename.split()
    # Order WEIGHT_NAMES so longest names are first
    for style in sorted(WEIGHT_NAMES, key=lambda k: len(k), reverse=True):
        if style in tokens:
            ttFont["OS/2"].usWeightClass = WEIGHT_NAMES[style]
            return ttFont["OS/2"].usWeightClass != old_weight_class

    if "Italic" in tokens:
        ttFont["OS/2"].usWeightClass = 400
        return ttFont["OS/2"].usWeightClass != old_weight_class
    raise ValueError(
        f"Cannot determine usWeightClass because font style, '{stylename}' "
        f"doesn't have a weight token which is in our known "
        f"weights, '{WEIGHT_NAMES.keys()}'"
    )


def fix_fs_selection(ttFont):
    """Fix the OS/2 table's fsSelection so it conforms to GF's supported
    styles table:
    https://github.com/googlefonts/gf-docs/tree/main/Spec#supported-styles

    Args:
        ttFont: a TTFont instance
    """
    stylename = font_stylename(ttFont)
    tokens = set(stylename.split())
    old_selection = fs_selection = ttFont["OS/2"].fsSelection

    # turn off all bits except for bit 7 (USE_TYPO_METRICS)
    fs_selection &= 1 << 7

    if "Italic" in tokens:
        fs_selection |= 1 << 0
    if "Bold" in tokens:
        fs_selection |= 1 << 5
    # enable Regular bit for all other styles
    if not tokens & set(["Bold", "Italic"]):
        fs_selection |= 1 << 6
    ttFont["OS/2"].fsSelection = fs_selection
    return old_selection != fs_selection


def fix_mac_style(ttFont):
    """Fix the head table's macStyle so it conforms to GF's supported
    styles table:
    https://github.com/googlefonts/gf-docs/tree/main/Spec#supported-styles

    Args:
        ttFont: a TTFont instance
    """
    old_mac_style = ttFont["head"].macStyle
    stylename = font_stylename(ttFont)
    tokens = set(stylename.split())
    mac_style = 0
    if "Italic" in tokens:
        mac_style |= 1 << 1
    if "Bold" in tokens:
        mac_style |= 1 << 0
    ttFont["head"].macStyle = mac_style
    return old_mac_style != mac_style


def fix_fvar_instances(ttFont, axis_dflts=None):
    """Replace a variable font's fvar instances with a set of new instances
    that conform to the Google Fonts instance spec:
    https://github.com/googlefonts/gf-docs/tree/main/Spec#fvar-instances

    Args:
        ttFont: a TTFont instance
    """
    if "fvar" not in ttFont:
        raise ValueError("ttFont is not a variable font")

    fvar = ttFont['fvar']
    old_instances = { (ttFont["name"].getDebugName(inst.subfamilyNameID) or "<removed>"): inst.coordinates for inst in fvar.instances }
    build_fvar_instances(ttFont, axis_dflts)
    new_instances = { ttFont["name"].getDebugName(inst.subfamilyNameID): inst.coordinates for inst in fvar.instances }
    if new_instances != old_instances:
        log.info("Set instances in fvar table to: %s", ", ".join(new_instances.keys()))
        log.info("(Old instances were: %s)", ", ".join(old_instances.keys()))
        log.info("Consider fixing export list in source\n")


def fix_nametable(ttFont):
    """Fix a static font's name table so it conforms to the Google Fonts
    supported styles table:
    https://github.com/googlefonts/gf-docs/tree/main/Spec#supported-styles

    Args:
        ttFont: a TTFont instance
    """
    old_nametable = {n.nameID: n.toStr() for n in ttFont["name"].names}
    build_name_table(ttFont)
    new_nametable = {n.nameID: n.toStr() for n in ttFont["name"].names}

    if old_nametable != new_nametable:
        log.info("Name table entries changed (consider fixing the source instead):")
        for nid, old_name in old_nametable.items():
            new_name = new_nametable.get(nid, "<removed>")
            if new_name != old_name:
                log.info("- %i: %s", nid, old_name)
                log.info("+ %i: %s", nid, new_name)
        log.info("")


def rename_font(font, new_name):
    nametable = font["name"]
    current_name = font_familyname(font)
    if not current_name:
        raise Exception(
            "Name table does not contain nameID 1 or nameID 16. "
            "This tool does not work on webfonts."
        )
    log.info("Updating font name records")
    build_name_table(font, family_name=new_name)


def fix_filename(ttFont):
    return build_filename(ttFont)


def inherit_vertical_metrics(ttFonts, family_name=None):
    """Inherit the vertical metrics from the same family which is
    hosted on Google Fonts.

    Args:
        ttFonts: a list of TTFont instances which belong to a family
        family_name: Optional string which allows users to specify a
            different family to inherit from e.g "Maven Pro".
    """
    family_name = font_familyname(ttFonts[0]) if not family_name else family_name

    gf_fonts = list(map(TTFont, download_family_from_Google_Fonts(family_name)))
    gf_fonts = {font_stylename(f): f for f in gf_fonts}
    # TODO (Marc F) use Regular font instead. If VF use font which has Regular
    # instance
    gf_fallback = list(gf_fonts.values())[0]

    fonts = {font_stylename(f): f for f in ttFonts}
    for style, font in fonts.items():
        if style in gf_fonts:
            src_font = gf_fonts[style]
        else:
            src_font = gf_fallback
        copy_vertical_metrics(src_font, font)

        if typo_metrics_enabled(src_font):
            font["OS/2"].fsSelection |= 1 << 7


def fix_vertical_metrics(ttFonts):
    """Fix a family's vertical metrics based on:
    https://github.com/googlefonts/gf-docs/tree/main/VerticalMetrics

    Args:
        ttFonts: a list of TTFont instances which belong to a family
    """
    src_font = next((f for f in ttFonts if font_stylename(f) == "Regular"), ttFonts[0])

    # TODO (Marc F) CJK Fonts?

    # If OS/2.fsSelection bit 7 isn't enabled, enable it and set the typo metrics
    # to the previous win metrics.
    if not typo_metrics_enabled(src_font):
        src_font["OS/2"].fsSelection |= 1 << 7  # enable USE_TYPO_METRICS
        src_font["OS/2"].sTypoAscender = src_font["OS/2"].usWinAscent
        src_font["OS/2"].sTypoDescender = -src_font["OS/2"].usWinDescent
        src_font["OS/2"].sTypoLineGap = 0

    # Set the hhea metrics so they are the same as the typo
    src_font["hhea"].ascent = src_font["OS/2"].sTypoAscender
    src_font["hhea"].descent = src_font["OS/2"].sTypoDescender
    src_font["hhea"].lineGap = src_font["OS/2"].sTypoLineGap

    # Set the win Ascent and win Descent to match the family's bounding box
    win_desc, win_asc = family_bounding_box(ttFonts)
    src_font["OS/2"].usWinAscent = win_asc
    src_font["OS/2"].usWinDescent = abs(win_desc)

    # Set all fonts vertical metrics so they match the src_font
    for ttFont in ttFonts:
        ttFont["OS/2"].fsSelection |= 1 << 7
        copy_vertical_metrics(src_font, ttFont)


def copy_vertical_metrics(src_font, dst_font):
    for table, key in [
        ("OS/2", "usWinAscent"),
        ("OS/2", "usWinDescent"),
        ("OS/2", "sTypoAscender"),
        ("OS/2", "sTypoDescender"),
        ("OS/2", "sTypoLineGap"),
        ("hhea", "ascent"),
        ("hhea", "descent"),
        ("hhea", "lineGap"),
    ]:
        val = getattr(src_font[table], key)
        setattr(dst_font[table], key, val)


def fix_italic_angle(ttFont):
    style_name = font_stylename(ttFont)
    if "Italic" not in style_name and ttFont["post"].italicAngle != 0:
        ttFont["post"].italicAngle = 0
        return True
    # TODO (Marc F) implement for italic fonts
    return False


def fix_ascii_fontmetadata(font):
    """Fixes TTF 'name' table strings to be ascii only"""
    for name in font['name'].names:
        title = name.string.decode(name.getEncoding())
        title = normalize_unicode_marks(title)
        name.string = title.encode(name.getEncoding())


def convert_cmap_subtables_to_v4(font):
  """Converts all cmap subtables to format 4.

  Returns a list of tuples (format, platformID, platEncID) of the tables
  which needed conversion."""
  cmap = font['cmap']
  outtables = []
  converted = []
  for table in cmap.tables:
    if table.format != 4:
      converted.append((table.format, table.platformID, table.platEncID))
    newtable = CmapSubtable.newSubtable(4)
    newtable.platformID = table.platformID
    newtable.platEncID = table.platEncID
    newtable.language = table.language
    newtable.cmap = table.cmap
    outtables.append(newtable)
  font['cmap'].tables = outtables
  return converted


def drop_nonpid0_cmap(font, report=True):
  keep, drop = partition_cmap(font, lambda table: table.platformID == 0, report)
  return drop


def drop_mac_cmap(font, report=True):
  keep, drop = partition_cmap(font, lambda table: table.platformID != 1 or table.platEncID != 0, report)
  return drop

def fix_pua(font):
    unencoded_glyphs = get_unencoded_glyphs(font)
    if not unencoded_glyphs:
        return

    ucs2cmap = None
    cmap = font["cmap"]

    # Check if an UCS-2 cmap exists
    for ucs2cmapid in ((3, 1), (0, 3), (3, 0)):
        ucs2cmap = cmap.getcmap(ucs2cmapid[0], ucs2cmapid[1])
        if ucs2cmap:
            break
    # Create UCS-4 cmap and copy the contents of UCS-2 cmap
    # unless UCS 4 cmap already exists
    ucs4cmap = cmap.getcmap(3, 10)
    if not ucs4cmap:
        cmapModule = getTableModule('cmap')
        ucs4cmap = cmapModule.cmap_format_12(12)
        ucs4cmap.platformID = 3
        ucs4cmap.platEncID = 10
        ucs4cmap.language = 0
        if ucs2cmap:
            ucs4cmap.cmap = deepcopy(ucs2cmap.cmap)
        cmap.tables.append(ucs4cmap)
    # Map all glyphs to UCS-4 cmap Supplementary PUA-A codepoints
    # by 0xF0000 + glyphID
    ucs4cmap = cmap.getcmap(3, 10)
    for glyphID, glyph in enumerate(font.getGlyphOrder()):
        if glyph in unencoded_glyphs:
            ucs4cmap.cmap[0xF0000 + glyphID] = glyph
    font['cmap'] = cmap
    return True


def fix_isFixedPitch(ttfont):

    same_width = set()
    glyph_metrics = ttfont['hmtx'].metrics
    messages = []
    changed = False
    for character in [chr(c) for c in range(65, 91)]:
        same_width.add(glyph_metrics[character][0])

    if len(same_width) == 1:
        if ttfont['post'].isFixedPitch == 1:
            messages.append("Skipping isFixedPitch is set correctly")
        else:
            messages.append("Font is monospace. Updating isFixedPitch to 0")
            ttfont['post'].isFixedPitch = 1
            changed = True

        familyType = ttfont['OS/2'].panose.bFamilyType
        if familyType == 2:
            expected = 9
        elif familyType == 3 or familyType == 5:
            expected = 3
        elif familyType == 0:
            messages.append("Font is monospace but panose fields seems to be not set."
                  " Setting values to defaults (FamilyType = 2, Proportion = 9).")
            ttfont['OS/2'].panose.bFamilyType = 2
            ttfont['OS/2'].panose.bProportion = 9
            changed = True
            expected = None
        else:
            expected = None

        if expected:
            if ttfont['OS/2'].panose.bProportion == expected:
                messages.append("Skipping OS/2.panose.bProportion is set correctly")
            else:
                messages.append(("Font is monospace."
                       " Since OS/2.panose.bFamilyType is {}"
                       " we're updating OS/2.panose.bProportion"
                       " to {}").format(familyType, expected))
                ttfont['OS/2'].panose.bProportion = expected
                changed = True

        widths = [m[0] for m in ttfont['hmtx'].metrics.values() if m[0] > 0]
        width_max = max(widths)
        if ttfont['hhea'].advanceWidthMax == width_max:
            messages.append("Skipping hhea.advanceWidthMax is set correctly")
        else:
            messsages.append("Font is monospace. Updating hhea.advanceWidthMax to %i" %
                  width_max)
            ttfont['hhea'].advanceWidthMax = width_max
            changed = True

        avg_width = otRound(sum(widths) / len(widths))
        if avg_width == ttfont['OS/2'].xAvgCharWidth:
            messages.append("Skipping OS/2.xAvgCharWidth is set correctly")
        else:
            messages.append("Font is monospace. Updating OS/2.xAvgCharWidth to %i" %
                  avg_width)
            ttfont['OS/2'].xAvgCharWidth = avg_width
            changed = True
    else:
        if ttfont['post'].isFixedPitch != 0 or ttfont['OS/2'].panose.bProportion != 0:
            changed = True
        ttfont['post'].isFixedPitch = 0
        ttfont['OS/2'].panose.bProportion = 0
    return changed, messages


def drop_superfluous_mac_names(ttfont):
    """Drop superfluous Mac nameIDs.

    The following nameIDS are kept:
    1: Font Family name,
    2: Font Family Subfamily name,
    3: Unique font identifier,
    4: Full font name,
    5: Version string,
    6: Postscript name,
    16: Typographic family name,
    17: Typographic Subfamily name
    18: Compatible full (Macintosh only),
    20: PostScript CID,
    21: WWS Family Name,
    22: WWS Subfamily Name,
    25: Variations PostScript Name Prefix.

    We keep these IDs in order for certain application to still function
    such as Word 2011. IDs 1-6 are very common, > 16 are edge cases.

    https://www.microsoft.com/typography/otspec/name.htm"""
    keep_ids = [1, 2, 3, 4, 5, 6, 16, 17, 18, 20, 21, 22, 25]
    changed = False
    for n in range(255):
        if n not in keep_ids:
            name = ttfont['name'].getName(n, 1, 0, 0)
            if name:
                changed = True
                ttfont['name'].names.remove(name)
    return changed


def drop_mac_names(ttfont):
    """Drop all mac names"""
    changed = False
    for n in range(255):
        name = ttfont['name'].getName(n, 1, 0, 0)
        if name:
            ttfont['name'].names.remove(name)
            changed = True
    return changed


def fix_colr_v0_gid1(ttfont):
    assert "COLR" in ttfont and ttfont["COLR"].version == 0
    if ttfont["maxp"].numGlyphs < 2:
        return ttfont
    glyph_names = ttfont.getGlyphOrder()
    glyf_table = ttfont["glyf"]
    second_glyph = glyph_names[1]
    if glyf_table[second_glyph].numberOfContours == 0:
        return ttfont
    has_empty_glyphs = any(glyf_table[g].numberOfContours == 0 for g in glyph_names)
    if has_empty_glyphs:
        fixed_font = _swap_empty_glyph_to_gid1(ttfont)
    else:
        fixed_font = _add_empty_glyph_to_gid1(ttfont)
    return fixed_font


def _swap_empty_glyph_to_gid1(ttfont):
    from nanoemoji.reorder_glyphs import reorder_glyphs
    from nanoemoji.util import load_fully
    glyf_table = ttfont["glyf"]
    ttfont = load_fully(ttfont)
    glyph_order = ttfont.getGlyphOrder()
    empty_glyph = next(
        (g for g in glyph_order if glyf_table[g].numberOfContours == 0),
        None
    )
    if empty_glyph is None:
        raise ValueError(
            "Font contains no empty glyphs. Please include a space or .null glyph"
        )
    new_order = list(glyph_order)
    new_order.remove(empty_glyph)
    new_order.insert(1, empty_glyph)
    reorder_glyphs(ttfont, new_order)
    return ttfont


def _add_empty_glyph_to_gid1(ttfont):
    from nanoemoji.util import load_fully
    from fontTools.ttLib.tables._g_l_y_f import Glyph
    from fontTools.ttLib.tables.otTables import NO_VARIATION_INDEX
    ttfont = load_fully(ttfont)
    glyph_order = ttfont.getGlyphOrder()
    glyf_table = ttfont["glyf"]
    hmtx = ttfont["hmtx"]

    empty_glyph = Glyph()
    empty_name = ".null" if ".null" not in glyph_order else "emptyglyph"
    assert empty_name not in glyph_order, f"{empty_name} already exists in font"
    glyf_table.glyphs[empty_name] = empty_glyph
    hmtx.metrics[empty_name] = (0, 0)
    if "HVAR" in ttfont:
        hvar = ttfont["HVAR"].table
        if hvar.AdvWidthMap:
            hvar.AdvWidthMap.mapping[empty_name] = NO_VARIATION_INDEX
        if hvar.LsbMap:
            hvar.LsbMap.mapping[empty_name] = NO_VARIATION_INDEX
        if hvar.RsbMap:
           hvar.RsbMap.mapping[empty_name] = NO_VARIATION_INDEX

    new_order = list(glyph_order)
    new_order.insert(1, empty_name)
    ttfont.setGlyphOrder(new_order)
    return ttfont


def fix_colr_v1_add_svg(ttfont):
    if "SVG " in ttfont:
        return ttfont
    font_filename = os.path.basename(ttfont.reader.file.name)
    with tempfile.TemporaryDirectory() as build_dir:
        subprocess.run(
            [
                "maximum_color",
                ttfont.reader.file.name,
                "--build_dir", build_dir,
                "--output_file", font_filename,
            ],
            check=True,
        )
        out_fp = os.path.join(build_dir, font_filename)
        fixed_ttfont = TTFont(out_fp)
        assert "SVG " in fixed_ttfont, "SVG table is missing"
        return fixed_ttfont


def fix_colr_font(ttfont: TTFont) -> TTFont:
    """For COLR v0 fonts, we need to ensure that the 2nd glyph is whitespace glyph,
    https://github.com/googlefonts/gftools/issues/609. For COLR v1 fonts, we need
    to run Nanoemoji's maximum_color script in order to generate an SVG table for
    applications that don't support COLRv1 yet,
    https://github.com/googlefonts/fontbakery/issues/3888.
    """
    assert "COLR" in ttfont, "Not a COLR font"
    colr_version = ttfont["COLR"].version
    if colr_version == 0:
        return fix_colr_v0_gid1(ttfont)
    elif colr_version == 1:
        return fix_colr_v1_add_svg(ttfont)
    else:
        raise NotImplementedError(f"COLR version '{colr_version}' not supported.")


def fix_font(font, include_source_fixes=False, new_family_name=None, fvar_instance_axis_dflts=None):
    if new_family_name:
        rename_font(font, new_family_name)
    if font["OS/2"].version > 1:
        font["OS/2"].version = 4

    if "fpgm" in font:
        fix_hinted_font(font)
    else:
        fix_unhinted_font(font)

    if "fvar" in font:
        name_table = font["name"]
        variation_ps_name = name_table.getName(25, 3, 1, 0x409)
        if not variation_ps_name:
            build_variations_ps_name(font)
            var_ps_name = font["name"].getName(25, 3, 1, 0x409).toUnicode()
            log.info(f"Added a Variations PostScript Name Prefix (NameID 25) '{var_ps_name}'")
    
    if "COLR" in font:
        log.info("Fixing COLR font")
        font = fix_colr_font(font)

    if include_source_fixes:
        remove_tables(font)
        fix_nametable(font)
        if fix_fs_type(font):
            log.info("Changed OS/2 table's fsType flag to 0 (Installable embedding)")
            log.info("Consider fixing in the source (e.g. adding a 'fsType' custom parameter in Glyphs)\n")
        if fix_fs_selection(font):
            log.info("Changed OS/2 table's fsSelection flag to %i", font["OS/2"].fsSelection)
            log.info("Consider fixing in the source (e.g. adding an 'openTypeOS2Selection' or 'Use Typo Metrics' custom parameter in Glyphs)\n")
        if fix_mac_style(font):
            log.info("Changed head table's macStyle to %i", font["head"].macStyle)
            log.info("Consider fixing in the source\n")
        if fix_weight_class(font):
            log.info("Changed OS/2 table's usWeightClass to %i", font["OS/2"].usWeightClass)
            log.info("Consider fixing in the source\n")
        if fix_italic_angle(font):
            log.info("Changed post table's italicAngle to %f", font["post"].italicAngle)
            log.info("Consider fixing in the source\n")

        if "fvar" in font:
            fix_fvar_instances(font, fvar_instance_axis_dflts)


def fix_family(fonts, include_source_fixes=False, new_family_name=None, fvar_instance_axis_dflts=None):
    """Fix all fonts in a family"""
    validate_family(fonts)

    for font in fonts:
        fix_font(
            font,
            include_source_fixes=include_source_fixes,
            new_family_name=new_family_name,
            fvar_instance_axis_dflts=fvar_instance_axis_dflts
        )
    family_name = font_familyname(fonts[0])
    if include_source_fixes:
        try:
            if Google_Fonts_has_family(family_name):
                inherit_vertical_metrics(fonts)
            else:
                log.warning(
                    f"{family_name} is not on Google Fonts. Skipping "
                    "regression fixes"
                )
        except FileNotFoundError:
            log.warning(
                f"Google Fonts api key not found so we can't regression "
                "fix fonts. See Repo readme to add keys."
            )
        fix_vertical_metrics(fonts)
        if all(["fvar" in f for f in fonts]):
            gen_stat_tables(fonts, ["opsz", "wdth", "wght", "ital", "slnt"])


class FontFixer():
    def __init__(self, path, report=True, verbose=False, **kwargs):
        self.font = TTFont(path)
        self.path = path
        self.font_filename = basename(path)
        self.saveit = False
        self.report = report
        self.verbose = verbose
        self.messages = []
        self.args = kwargs
        self.fixes = []
        if "fixes" in kwargs:
            self.fixes = kwargs["fixes"]

    def __del__(self):
        if self.report:
            print("\n".join(self.messages))
        if self.saveit:
            if self.verbose:
                print('Saving %s to %s.fix' % (self.font_filename, self.path))
            self.font.save(self.path + ".fix")
        elif self.verbose:
            print('There were no changes needed on %s!' % self.font_filename)

    def show(self):
        pass

    def fix(self):
        for f in self.fixes:
            rv = f(self.font)
            if isinstance(rv, tuple) and len(rv) == 2:
                changed, messages = rv
                self.messages.extend(messages)
            else:
                changed = rv
            if changed:
                self.saveit = True


class GaspFixer(FontFixer):

    def fix(self, value=15):
        try:
            table = self.font.get('gasp')
            table.gaspRange[65535] = value
            self.saveit = True
        except:
            print(('ER: {}: no table gasp... '
                  'Creating new table. ').format(self.path))
            table = ttLib.newTable('gasp')
            table.gaspRange = {65535: value}
            self.font['gasp'] = table
            self.saveit = True

    def show(self):
        try:
            self.font.get('gasp')
        except:
            print('ER: {}: no table gasp'.format(self.path))
            return

        try:
            print(self.font.get('gasp').gaspRange[65535])
        except IndexError:
            print('ER: {}: no index 65535'.format(self.path))

