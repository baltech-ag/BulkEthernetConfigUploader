import pytest

from becu.core.bec_file import load_bec
from becu.core.errors import BecFormatError

SAMPLE = ('<?xml version="1.0" encoding="UTF-8"?>\n'
          '<bec configId="99900-0002-0000-01" configName="Config B" customerKeySignature="123456789" '
          'fileversion="2"><block content="01 89 1D 15" version="2" />'
          '<block content="AABBCC" /><block content="0a 0b\n0c" version="0" /></bec>')


def test_load_bec(tmp_path):
    path = tmp_path / "c.bec"
    path.write_text(SAMPLE, encoding="utf-8")
    bec = load_bec(path)
    assert bec.config_id == "99900-0002-0000-01"
    assert bec.config_name == "Config B"
    assert bec.customer_key_signature == 123456789
    assert bec.block_count == 3 and bec.payload_size == 10
    assert [b.version for b in bec.blocks] == [2, 0, 0]
    assert bec.blocks[0].data == bytes.fromhex("01891D15")
    assert bec.matches("99900-0002-0000-01") and not bec.matches("99900-0002-0000-00") and not bec.matches(None)
    assert not bec.is_draft


@pytest.mark.parametrize("xml, message", [
    ("<cfg fileversion='2'/>", "root element"),
    ("<bec fileversion='3'><block content='00'/></bec>", "fileversion"),
    ("<bec fileversion='2' configName='x'><block content='00'/></bec>", "configId"),
    ("<bec fileversion='2' configId='1-2' configName='x'><block content='00'/></bec>", "form"),
    ("<bec fileversion='2' configId='99900-0002-0000-01' configName='x'></bec>", "no <block>"),
    ("<bec fileversion='2' configId='99900-0002-0000-01' configName='x'><block content='0G'/></bec>", "hex"),
    ("<bec fileversion='2' configId='99900-0002-0000-01' configName='x'><block content=''/></bec>", "empty"),
    ("<bec fileversion='2' configId='99900-0002-0000-01' configName='x' customerKeySignature='x'>"
     "<block content='00'/></bec>", "customerKeySignature"),
    ("<bec", "invalid XML"),
])
def test_invalid_bec(tmp_path, xml, message):
    path = tmp_path / "bad.bec"
    path.write_text(xml, encoding="utf-8")
    with pytest.raises(BecFormatError, match=message):
        load_bec(path)


def test_fileversion_1_and_draft(tmp_path):
    path = tmp_path / "v1.bec"
    path.write_text("<bec fileversion='1'><block content='00' version='2'/></bec>", encoding="utf-8")
    bec = load_bec(path)
    assert bec.config_id == "99999-9999-9999-99" and bec.config_name == "unknown"
    draft = tmp_path / "d.bec"
    draft.write_text("<bec fileversion='2' configId='99900-0002-0000-DRAFT' configName='d'><block content='00'/></bec>",
                     encoding="utf-8")
    assert load_bec(draft).is_draft


def test_missing_file(tmp_path):
    with pytest.raises(BecFormatError):
        load_bec(tmp_path / "none.bec")
