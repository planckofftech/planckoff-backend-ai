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


def test_tag_column_defaults_to_first_when_absent():
    assert tag_column_index([None, "from_space"]) == 0
    assert tag_column_index(["from_space", "door_tag"]) == 1
