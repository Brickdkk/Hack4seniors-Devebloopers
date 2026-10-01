import pytest

from app.config import bare_name, format_rut


@pytest.mark.parametrize("raw,expected", [("123456785", "12.345.678-5"), ("12345678-5", "12.345.678-5"), ("12.345.678-5", "12.345.678-5"),
                                          ("7.654.321-k", "7.654.321-K"), ("", ""), ("no es un rut", "no es un rut")])
def test_saved_rut_is_normalized(raw, expected):
    assert format_rut(raw) == expected


@pytest.mark.parametrize("raw,expected", [("don Eliecer", "Eliecer"), ("Doña Rosa", "Rosa"), ("  don  René ", "René"), ("Eliecer", "Eliecer"), ("Donoso", "Donoso"), ("", "")])
def test_bare_name_drops_the_honorific(raw, expected):
    assert bare_name(raw) == expected
