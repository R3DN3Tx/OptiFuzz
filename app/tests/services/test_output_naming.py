from backend.services.output_naming import build_output_stem

DEFAULT = "dirsearch_{target}_{port}_{path}"


def test_default_template_matches_the_requested_scheme() -> None:
    stem = build_output_stem(
        DEFAULT, target_name="api.example.com", url="http://127.0.0.1:8910/admin/subfolder", path="/admin/subfolder"
    )

    assert stem == "dirsearch_api.example.com_8910_admin_subfolder"


def test_root_path_becomes_root() -> None:
    stem = build_output_stem(DEFAULT, target_name="t", url="http://127.0.0.1:8910/", path="/")

    assert stem == "dirsearch_t_8910_root"


def test_https_without_explicit_port_defaults_to_443() -> None:
    stem = build_output_stem(DEFAULT, target_name="t", url="https://example.com/", path="/x")

    assert stem == "dirsearch_t_443_x"


def test_http_without_explicit_port_defaults_to_80() -> None:
    stem = build_output_stem(DEFAULT, target_name="t", url="http://example.com/", path="/x")

    assert stem == "dirsearch_t_80_x"


def test_unsafe_characters_in_target_name_are_sanitized() -> None:
    stem = build_output_stem(DEFAULT, target_name="my target / v2", url="http://h:8080/", path="/x")

    assert stem == "dirsearch_my_target_v2_8080_x"


def test_malformed_template_falls_back_to_default_instead_of_raising() -> None:
    stem = build_output_stem("{nope}", target_name="t", url="http://h:80/", path="/x")

    assert stem == "dirsearch_t_80_x"


def test_custom_template_is_honored() -> None:
    stem = build_output_stem("{path}_{target}", target_name="t", url="http://h:80/", path="/x")

    assert stem == "x_t"


# Real, reported bug: fuzzing a long discovered path made the job fail
# outright on Windows -- the fuzzed path went straight into this output
# filename with no length cap, and the full on-disk path crossed Windows'
# MAX_PATH (260 chars). The actual MAX_PATH fix now lives in
# job_manager/manager.py (a short working name while dirsearch writes,
# renamed to this module's full name via the \\?\ prefix once the job is
# done) -- so this module's own cap only has to guard against the ONE thing
# that fix can't route around: NTFS's ~255-char single-*component* limit
# (independent of total path length). See output_naming.py's own comment.

def test_a_realistically_long_fuzzed_path_is_shown_in_full_not_truncated() -> None:
    # The whole point of the working-name/rename split in manager.py: a
    # pentester wants to actually SEE the fuzzed path in the filename, not
    # a truncated-with-a-hash one, for any realistic case -- only a
    # genuinely extreme one (test below) should still get cut.
    realistic_path = "/api/v2/users/12345678-1234-1234-1234-123456789012/documents/98765432-4321-4321-4321-210987654321"
    assert 80 < len(realistic_path) < 150

    stem = build_output_stem(DEFAULT, target_name="t", url="http://h:80/", path=realistic_path)

    expected_path_segment = realistic_path.strip("/").replace("/", "_")
    assert stem == f"dirsearch_t_80_{expected_path_segment}"  # present in full, no truncation/hash suffix


def test_a_very_long_fuzzed_path_is_capped_not_left_to_blow_the_ntfs_component_limit() -> None:
    long_path = "/" + "/".join(f"segment-{i}-with-some-extra-length-padding" for i in range(20))
    assert len(long_path) > 300  # a genuinely extreme, not just long, route

    stem = build_output_stem(DEFAULT, target_name="t", url="http://h:80/", path=long_path)

    assert len(stem) <= 200


def test_a_very_long_target_name_is_capped_too() -> None:
    long_name = "client-" + "x" * 300

    stem = build_output_stem(DEFAULT, target_name=long_name, url="http://h:80/", path="/x")

    assert len(stem) <= 200


def test_two_different_long_paths_never_collide_onto_the_same_stem() -> None:
    # A bare truncation could make two DIFFERENT long paths that happen to
    # share the same prefix collide onto the same output file, silently
    # overwriting one fuzz's results with the other's.
    base = "segment-with-some-shared-prefix-" * 5
    path_a = f"/{base}alpha"
    path_b = f"/{base}bravo"

    stem_a = build_output_stem(DEFAULT, target_name="t", url="http://h:80/", path=path_a)
    stem_b = build_output_stem(DEFAULT, target_name="t", url="http://h:80/", path=path_b)

    assert stem_a != stem_b


def test_a_custom_template_combining_several_long_fields_is_still_capped() -> None:
    # Individual {target}/{path} fields are capped on their own, but a
    # custom template (Settings -> Output filenames) could still combine
    # several of them (or add its own long literal text) into something
    # longer than any single field's own cap -- the final stem-level safety
    # net has to catch that too.
    stem = build_output_stem(
        "{path}_{target}_{path}_{target}",
        target_name="client-" + "x" * 100,
        url="http://h:80/",
        path="/" + "y" * 100,
    )

    assert len(stem) <= 200


def test_short_values_are_never_truncated_or_hash_suffixed() -> None:
    # The cap must be invisible for the overwhelming common case -- a short,
    # normal path shouldn't grow a hash suffix it doesn't need.
    stem = build_output_stem(
        DEFAULT, target_name="api.example.com", url="http://127.0.0.1:8910/admin/subfolder", path="/admin/subfolder"
    )

    assert stem == "dirsearch_api.example.com_8910_admin_subfolder"
