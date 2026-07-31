from app.core.header_mapper import extra_key, map_headers, tag_column_index


def test_ellis_headers_map_completely():
    headers = ["#", "From", "To", "Width", "Height", "Type", "Material", "Finish",
               "Frame Material", "Frame Finish", "THRESHOLD", "F_R", "HW", "Comments"]
    mapped, unmapped = map_headers(headers)
    assert unmapped == []
    assert mapped == [
        "door_tag", "from_space", "to_space", "door_width", "door_height",
        "door_type", "door_material", "door_finish", "frame_material",
        "frame_finish", "threshold", "fire_rating", "hw_set", "comments",
    ]


def test_qualified_headers_beat_bare_ones():
    """"FINISH" must not swallow "FRAME FINISH", or every frame column lands in
    the door column."""
    mapped, _ = map_headers(["Finish", "Frame Finish", "Material", "Frame Material"])
    assert mapped == ["door_finish", "frame_finish", "door_material", "frame_material"]


def test_column_order_does_not_change_the_mapping():
    mapped, _ = map_headers(["Frame Finish", "Finish", "Frame Material", "Material"])
    assert mapped == ["frame_finish", "door_finish", "frame_material", "door_material"]


def test_hardware_synonyms_all_reach_hw_set():
    for alias in ["HW", "HDW", "HDWE SET", "HARDWARE GROUP", "HW SET"]:
        mapped, _ = map_headers([alias])
        assert mapped == ["hw_set"], alias


def test_fire_rating_survives_encoding_variants():
    for alias in ["F.R", "F_R", "FR", "RATING", "FIRE RATING", "LABEL"]:
        mapped, _ = map_headers([alias])
        assert mapped == ["fire_rating"], alias


def test_unrecognized_headers_are_reported_never_dropped():
    mapped, unmapped = map_headers(["#", "ACOUSTIC RATING", "Comments"])
    assert mapped == ["door_tag", None, "comments"]
    assert unmapped == ["ACOUSTIC RATING"]
    assert extra_key("ACOUSTIC RATING", 1) == "acoustic_rating"


def test_duplicate_header_does_not_overwrite_the_first():
    mapped, unmapped = map_headers(["Finish", "Finish"])
    assert mapped[0] == "door_finish"
    assert mapped[1] is None
    assert unmapped == ["Finish"]


def test_grouped_headers_disambiguate_repeated_sub_headings():
    """Sheets that group columns under PANEL and FRAME print "MAT'L" twice.
    Once the group heading is pushed down onto its columns, the two are
    distinguishable; without it, the second MAT'L is an unmappable duplicate and
    frame_material comes back empty."""
    headers = ["NO.", "LOCATION", "TYPE", "PANEL MAT'L", "PANEL WIDTH",
               "PANEL HEIGHT", "PANEL THK", "FRAME MAT'L", "FRAME TYPE",
               "FRAME GAUGE", "FRAME WIDTH", "FIRE RATING", "HW SET", "NOTES"]
    mapped, _ = map_headers(headers)
    by_field = {f: headers[i] for i, f in enumerate(mapped) if f}

    assert by_field["door_material"] == "PANEL MAT'L"
    assert by_field["frame_material"] == "FRAME MAT'L"
    assert by_field["door_width"] == "PANEL WIDTH"
    assert by_field["door_height"] == "PANEL HEIGHT"
    assert by_field["hw_set"] == "HW SET"
    assert by_field["comments"] == "NOTES"

    # A frame's width and type must not be mistaken for the door's.
    assert mapped[headers.index("FRAME WIDTH")] is None
    assert mapped[headers.index("FRAME TYPE")] is None
    assert mapped[headers.index("PANEL THK")] is None


def test_overrides_fill_gaps_but_never_displace_the_alias_table():
    """Applied first, an override like "FRAME TYPE -> frame_material" claims the
    field and locks out "FRAME MAT'L", which genuinely matches. On a real sheet
    that put the frame type code into frame_material and the actual frame
    material into extras."""
    headers = ["FRAME TYPE", "FRAME MAT'L", "SPECIAL COL"]
    overrides = {"FRAME TYPE": "frame_material", "SPECIAL COL": "comments"}

    mapped, unmapped = map_headers(headers, overrides)

    assert mapped[1] == "frame_material", "alias table lost its column"
    assert mapped[0] is None, "override displaced a genuine alias match"
    assert mapped[2] == "comments", "override should still fill a real gap"
    assert unmapped == ["FRAME TYPE"]


def test_overrides_are_ignored_for_unknown_fields():
    mapped, _ = map_headers(["WEIRD"], {"WEIRD": "not_a_field"})
    assert mapped == [None]


def test_tag_column_defaults_to_first_when_absent():
    assert tag_column_index([None, "from_space"]) == 0
    assert tag_column_index(["from_space", "door_tag"]) == 1
