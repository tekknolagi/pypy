import sys, random
from pypy.rlib import runicode

class UnicodeTests(object):
    def typeequals(self, x, y):
        assert x == y
        assert type(x) is type(y)

    def checkdecode(self, s, encoding):
        decoder = getattr(runicode, "str_decode_%s" % encoding.replace("-", ""))
        if isinstance(s, str):
            trueresult = s.decode(encoding)
        else:
            trueresult = s
            s = s.encode(encoding)
        result, consumed = decoder(s, len(s), True)
        assert consumed == len(s)
        self.typeequals(trueresult, result)

    def checkencode(self, s, encoding):
        encoder = getattr(runicode,
                          "unicode_encode_%s" % encoding.replace("-", ""))
        if isinstance(s, unicode):
            trueresult = s.encode(encoding)
        else:
            trueresult = s
            s = s.decode(encoding)
        result = encoder(s, len(s), True)
        self.typeequals(trueresult, result)

    def checkencodeerror(self, s, encoding, start, stop):
        called = [False]
        def errorhandler(errors, enc, msg, t, startingpos,
                         endingpos, decode):
            called[0] = True
            assert errors == "foo!"
            assert enc == encoding
            assert t is s
            assert start == startingpos
            assert stop == endingpos
            assert not decode
            return "42424242", stop
        encoder = getattr(runicode,
                          "unicode_encode_%s" % encoding.replace("-", ""))
        result = encoder(s, len(s), "foo!", errorhandler)
        assert called[0]
        assert "42424242" in result

    def checkdecodeerror(self, s, encoding, start, stop, addstuff=True):
        called = [0]
        def errorhandler(errors, enc, msg, t, startingpos,
                         endingpos, decode=True):
            called[0] += 1
            if called[0] == 1:
                assert errors == "foo!"
                assert enc == encoding
                assert t is s
                assert start == startingpos
                assert stop == endingpos
                assert decode
                return u"42424242", stop
            return "", endingpos
        decoder = getattr(runicode,
                          "str_decode_%s" % encoding.replace("-", ""))
        if addstuff:
            s += "some rest in ascii"
        result, _ = decoder(s, len(s), "foo!", True, errorhandler)
        assert called[0] > 0
        assert "42424242" in result
        if addstuff:
            assert result.endswith(u"some rest in ascii")


class TestDecoding(UnicodeTests):
    
    # XXX test bom recognition in utf-16
    # XXX test proper error handling

    def test_all_ascii(self):
        for i in range(128):
            for encoding in "utf8 latin1 ascii".split():
                self.checkdecode(chr(i), encoding)

    def test_all_first_256(self):
        for i in range(256):
            for encoding in "utf8 latin1 utf16 utf-16-be utf-16-le".split():
                self.checkdecode(unichr(i), encoding)

    def test_random(self):
        for i in range(10000):
            uni = unichr(random.randrange(sys.maxunicode))
            for encoding in "utf8 utf16 utf-16-be utf-16-le".split():
                self.checkdecode(unichr(i), encoding)

    def test_single_chars_utf8(self):
        for s in ["\xd7\x90", "\xd6\x96", "\xeb\x96\x95", "\xf0\x90\x91\x93"]:
            self.checkdecode(s, "utf8")

    def test_utf8_errors(self):
        for s in [# unexpected end of data
                  "\xd7", "\xd6", "\xeb\x96", "\xf0\x90\x91"]:
            self.checkdecodeerror(s, "utf8", 0, len(s), addstuff=False)
            
        for s in [# unexpected code byte
                  "\x81", "\xbf",
                  # invalid data 2 byte
                  "\xd7\x50", "\xd6\x06", "\xd6\xD6",
                  # invalid data 3 byte
                  "\xeb\x56\x95", "\xeb\x06\x95", "\xeb\xD6\x95",
                  "\xeb\x96\x55", "\xeb\x96\x05", "\xeb\x96\xD5",
                  # invalid data 4 byte
                  "\xf0\x50\x91\x93", "\xf0\x00\x91\x93", "\xf0\xd0\x91\x93", 
                  "\xf0\x90\x51\x93", "\xf0\x90\x01\x93", "\xf0\x90\xd1\x93", 
                  "\xf0\x90\x91\x53", "\xf0\x90\x91\x03", "\xf0\x90\x91\xd3", 
                  ]:
            self.checkdecodeerror(s, "utf8", 0, len(s), addstuff=True)

    def test_ascii_error(self):
        self.checkdecodeerror("abc\xFF\xFF\xFFcde", "ascii", 3, 4)


class TestEncoding(UnicodeTests):
    def test_all_ascii(self):
        for i in range(128):
            for encoding in "utf8 latin1 ascii".split():
                self.checkencode(unichr(i), encoding)

    def test_all_first_256(self):
        for i in range(256):
            for encoding in "utf8 latin1 utf16 utf-16-be utf-16-le".split():
                self.checkencode(unichr(i), encoding)

    def test_random(self):
        for i in range(10000):
            uni = unichr(random.randrange(sys.maxunicode))
            for encoding in "utf8 utf16 utf-16-be utf-16-le".split():
                self.checkencode(unichr(i), encoding)

    def test_single_chars_utf8(self):
        for s in ["\xd7\x90", "\xd6\x96", "\xeb\x96\x95", "\xf0\x90\x91\x93"]:
            self.checkencode(s, "utf8")

    def test_ascii_error(self):
        self.checkencodeerror(u"abc\xFF\xFF\xFFcde", "ascii", 3, 6)

    def test_latin1_error(self):
        self.checkencodeerror(u"abc\uffff\uffff\uffffcde", "latin1", 3, 6)
