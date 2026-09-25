"""标识与缓存键测试（方案 §6.2）：resume_id 基于内容 hash，run_id 每次唯一。"""

from __future__ import annotations

from resume_screening.core.ids import file_content_hash, new_run_id, resume_id_of


def test_same_content_same_id(tmp_path) -> None:
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"resume-bytes")
    b.write_bytes(b"resume-bytes")
    assert resume_id_of(a) == resume_id_of(b)
    assert file_content_hash(a) == file_content_hash(b)


def test_different_content_different_id(tmp_path) -> None:
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"resume-bytes")
    b.write_bytes(b"resume-bytes-v2")
    assert resume_id_of(a) != resume_id_of(b)


def test_copy_in_other_folder_keeps_id(tmp_path) -> None:
    """换目录不应导致重复解析——resume_id 只认内容，不认路径。"""
    a = tmp_path / "a.pdf"
    (tmp_path / "sub").mkdir()
    c = tmp_path / "sub" / "a_copy.pdf"
    a.write_bytes(b"x" * 2048)
    c.write_bytes(b"x" * 2048)
    assert resume_id_of(a) == resume_id_of(c)


def test_run_id_unique() -> None:
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50
