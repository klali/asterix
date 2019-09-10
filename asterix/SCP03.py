""" asterix/SCP03.py

__author__ = "Petr Tobiska"

Author: Petr Tobiska, mailto:petr.tobiska@gmail.com
Date: 2015-10-15

This file is part of asterix, a framework for  communication with smartcards
 based on pyscard. This file implements SCP03 as defined in Global Platform.

asterix is free software; you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as published by
the Free Software Foundation; either version 2.1 of the License, or
(at your option) any later version.

asterix is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License
along with pyscard; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

References:
[GP CS] GlobalPlatform Card Specification, Version 2.2.1, Jan 2011
[GP AmD] Secure Channel Protocol 03, Card Specification v 2.2 - Amendment D,
         Version 1.1, Sep 2009

"""
from binascii import hexlify, unhexlify
from struct import pack, unpack
import unittest
# PyCrypto
from Crypto.Cipher import AES
# pyscard
try:
    from smartcard.CardConnectionDecorator import CardConnectionDecorator
    __all__ = ('DDC', 'SCP03', 'SCP03Connection')
except ImportError:
    __all__ = ('DDC', 'SCP03')
# asterix
from formutil import l2s, s2l, s2int, pad80, unpad80, partition
from GAF import GAF

INS_INIT_UPDATE = 0x50
INS_EXT_AUTH    = 0x82
INS_BEGIN_RMAC  = 0x7A
INS_END_RMAC    = 0x78

# masks for "i" parameter
M_PSEUDO     = 0x10    # 1/0: Pseudo-random / random card challenge
M_RMAC       = 0x20    # R-MAC, no R-ENC
M_RMACENC    = 0x60    # both R-MAC and R-MAC
# mask for SL
SL_CMAC         = 0x01
SL_CENC         = 0x02
SL_RMAC         = 0x10
SL_RENC         = 0x20


class DDC(object):
    """ Data derivation constants. """
    CardCrypto    = 0x00
    HostCrypto    = 0x01
    CardChallenge = 0x02
    S_ENC         = 0x04
    S_MAC         = 0x06
    S_RMAC        = 0x07


def logCh(CLA):
    """ Take log. channel from CLA byte """
    if CLA & 0x40:
        return 4 + CLA & 0x0F
    else:
        return CLA & 0x03


def CMAC(key, data):
    """ Calculate CMAC using AES as underlaying cipher.
key     -  a key used for CMAC calculation (string 16, 24 or 32B long)
data    - data to be signed (string)
Returns CMAC as a string 16B long. """
    IrrPoly = 0x87     # irr. polynomial for F2m, m = 128
    BS = 16            # block size of AES

    def polyMulX(poly):
        """Interpret value as a polynomial over F2m and multiply by the
polynomial x (modulo irreducible polynomial)"""
        vals = list(unpack(">qq", poly))
        carry = vals[1] < 0 and 1 or 0
        vals[1] = ((vals[1] << 1) % 0x10000000000000000)
        if vals[0] < 0:
            vals[1] ^= IrrPoly
        vals[0] = ((vals[0] << 1) % 0x10000000000000000) + carry
        return pack(">QQ", *vals)
    kcv = AES.new(key, AES.MODE_ECB).encrypt('\0'*BS)
    xorKey1 = polyMulX(kcv)
    xorKey2 = polyMulX(xorKey1)
    odata = [ord(x) for x in data]
    sLB = len(data) % BS
    if(sLB > 0 or len(data) == 0):
        odata += [0x80] + [0]*(BS-sLB-1)
        xorkey = xorKey2
    else:
        xorkey = xorKey1
    for i in xrange(BS):
        odata[-BS+i] ^= ord(xorkey[i])
    data = ''.join([chr(x) for x in odata])
    cipher = AES.new(key, AES.MODE_CBC, IV='\0'*BS)
    sig = cipher.encrypt(data)[-BS:]
    return sig


def KDF(key, const, L, context):
    """ Key derivation scheme as defined in [GP AmD] 4.1.5
key      - a key used for CMAC calculation (string 16, 24 or 32B long)
const    - a constant from DDC (u8)
L        - bit length of required output (u16)
context  - a context entering calculation (string)
Returns derived data as string L/8 bytes long."""
    nbl = (L + 127) / 128
    res = ''
    for i in range(1, nbl+1):
        data = '\0'*11 + pack(">BBHB", const, 0, L, i) + context
        res += CMAC(key, data)
    BL = L / 8
    return res[:BL]


class DEK(object):
    """ Representation of DEK for en/de-cryption of sensitive data"""
    def __init__(self, keyValue):
        self.keyValue = keyValue
        self.zAES = True
        assert len(keyValue) in (16, 24, 32), "Wrong length of AES key"

    def encrypt(self, data):
        """Encrypt sensitive data by DEK.
If data not BS-alligned, they are padded by '80..00'"""
        l = len(data) % 16
        if l > 0:
            data += '\x80' + '\0'*(15-l)
        key = AES.new(self.keyValue, AES.MODE_CBC, IV='\0'*16)
        return key.encrypt(data)

    def decrypt(self, data):
        """Decrypt sensitive data by DEK. Data must be BS-alligned.
No padding removed."""
        assert len(data) % 16 == 0, "Data length not multiple of 16"
        key = AES.new(self.keyValue, AES.MODE_CBC, IV='\0'*16)
        return key.decrypt(data)


class SCP03:
    """ Implementation of SCP03 calculation. """

    def __init__(self, **kw):
        """Constructor of SCP02 object.
Expected parameters (in dict):
  i            - parameter of SCP03, (u8, default M_PSEUDO)
  SD_AID       - AID of security domain to authenticate to (string,
                 default unhexlify('A000000151000000'))
  keyENC, keyMAC, keyDEK - static keys (strings 16, 24,  32B long)
  keyVer       - key version, (u8), default 0x30
  seqCounter   - sequence counter, (u24, default 0x000000)
  diverData    - bytes 1-10 of Init Update response (string 10B long,
                 default '\0'*10)
"""
        i = kw.get('i', 0x70)  # default value
        i %= 0x100
        assert i & ~(M_PSEUDO | M_RMACENC) == 0, "RFU bits nonzero"
        assert i != M_RMACENC ^ M_RMAC, "RENC without RMAC"
        self.i = i

        self.SD_AID = kw.get('SD_AID', unhexlify('A000000151000000'))
        assert 5 <= len(self.SD_AID) and len(self.SD_AID) <= 16, \
            "Wrong AID length: %d" % len(self.SD_AID)

        for k in ('keyENC', 'keyMAC', 'keyDEK'):
            assert k in kw, "Mandatory key %s missing" % k
            assert len(kw[k]) in (16, 24, 32), \
                "Wrong %s length: %d" % (k, len(kw[k]))
            self.__dict__[k] = kw[k]

        keyVer = kw.get('keyVer', 0x30)
        self.keyVer = keyVer

        seqCounter = kw.get('seqCounter', 0)
        assert 0 <= seqCounter and seqCounter < 0x1000000, \
            "Wrong seq. counter value %X" % seqCounter
        self.seqCounter = seqCounter

        self.diverData = kw.get('diverData', '\0'*10)
        assert len(self.diverData) == 10, \
            "Wrong length of diver. data: %d" % len(self.diverData)

    def initUpdate(self, host_challenge='\0'*8, logCh=0):
        """ Return APDU for Initial Update (as list[u8]).
Parameters:
    host_challenge (optional, default '0000000000000000')
    logCh - logical channel (optional, default 0)
 """
        assert 0 <= logCh and logCh < 20, "Wrong log. channel: %d" % logCh
        self.logCh = logCh
        assert len(host_challenge) == 8, \
            "Wrong length of host challenge: %d" % len(host_challenge)
        self.host_challenge = host_challenge

        apdu = [self.CLA(False), INS_INIT_UPDATE, self.keyVer, 0, 8] +\
               [ord(c) for c in self.host_challenge]
        return apdu

    def initUpdateResp(self, card_challenge=None):
        """ Return expected response to Initial Update (as string).
  card_challenge - card challenge if i & M_PSEUDO == 0 """
        self.deriveKeys(card_challenge)
        resp = self.diverData + pack("BBB", self.keyVer, 3, self.i) +\
            self.card_challenge + self.card_cryptogram
        if self.i & M_PSEUDO:
            resp += pack(">L", self.seqCounter)[1:]
        return resp

    def parseInitUpdate(self, apdu):
        """ Parse Init Update APDU (list[u8]) and if correct, set
log. channel and host challenge from it. """
        cla = apdu[0]
        assert 0x80 <= cla <= 0x83 or 0xC0 <= cla < 0xCF, "Wrong CLA"
        assert apdu[1] == INS_INIT_UPDATE, "Wrong INS"
        assert apdu[2] == self.keyVer, "Key version changed"
        # ignore P2?
        assert apdu[4] == len(apdu) - 5 == 8, "Wrong Lc/data length"
        self.logCh = logCh(cla)
        self.host_challenge = l2s(apdu[5:])

    def parseInitUpdateResp(self, resp):
        """ Parse response to Init Update and if correct set diverData,
seqCounter, and card_challenge from it.
resp     - response (list[u8])
Raise exception in case of wrong response. """
        assert len(resp) in (29, 32), \
            "Wrong length of response data to Init Update: %d" % len(resp)
        diverData, keyInfo, card_chal, card_cryptogram, seqCounter =\
            partition(l2s(resp), (10, 13, 21, 29))
        kv, i = ord(keyInfo[0]), ord(keyInfo[2])
        assert keyInfo[1] == chr(0x03), \
            "Wrong SCP number in resp. to Init Update %02X" % ord(keyInfo[0])
        assert i & ~(M_PSEUDO | M_RMACENC) == 0 \
            and i != M_RMACENC ^ M_RMAC, "Wrong SCP03 parameter %02X" % i
        self.i, self.keyVer, self.diverData = i, kv, diverData

        if self.i & M_PSEUDO:
            assert len(seqCounter) == 3, "Missing seq. counter"
            self.seqCounter = s2int(seqCounter)
        else:
            assert len(seqCounter) == 0, "Seq. counter shall not be present"

        self.deriveKeys(card_chal)
        assert card_cryptogram == self.card_cryptogram, \
            "Recieved and calculated card cryptogram difer: %s vs. %s" % \
            (hexlify(card_cryptogram), hexlify(self.card_cryptogram))

    def extAuth(self, SL=1):
        """ Build and retrun Ext Auth APDU. """
        if SL & SL_RMAC:
            assert self.i & M_RMAC, "R-MAC not in SCP parameter"
        if SL & SL_RENC:
            assert self.i & M_RMACENC == M_RMACENC, \
                "R-ENC not in SCP parameter"
        assert SL in (0, SL_CMAC, SL_CMAC | SL_CENC, SL_CMAC | SL_RMAC,
                      SL_CMAC | SL_CENC | SL_RMAC,
                      SL_CMAC | SL_CENC | SL_RMAC | SL_RENC), \
            "Wrong SL %02X" % SL
        self.SL = SL
        self.rmacSL = 0          # 0x10 or 0x30 after BEGIN R-MAC
        self.cmdCount = 0L       # command counter for C-ENC ICV

        if 'host_cryptogram' not in self.__dict__:
            self.deriveKeys(None)
        data2sign = '\0'*16 + pack("BBBBB", 0x84, INS_EXT_AUTH, SL, 0, 0x10)\
                    + self.host_cryptogram
        self.MACchain = CMAC(self.SMAC, data2sign)
        apdu = [self.CLA(), INS_EXT_AUTH, SL, 0, 0x10] + \
               [ord(x) for x in (self.host_cryptogram + self.MACchain[:8])]
        return apdu

    def parseExtAuth(self, apdu):
        """ Parse Ext Auth APDU (as hexstring) and
 check host cryptogram and MAC. """
        assert len(apdu) == 21, "Wrong data length"
        wapdu = self.extAuth(SL=apdu[2])
        assert apdu[:5] == wapdu[:5], "Wrong APDU header"
        assert apdu[5:13] == wapdu[5:13], "Wrong host cryptogram"
        assert apdu[13:] == wapdu[13:], "Wrong MAC"
        self.SL = apdu[2]

    def beginRMAC(self, rmacSL, saltData=None):
        """ Build BEGIN R-MAC APDU (list[u8]).
rmacSL - required SL, i.e. 0x10 for R-MAC and 0x30 for R-MAC and R-ENC
saltData - data to be added to APDU (as #(saltData))
Increase cmdCount."""
        # required rmacSL supported by SCP parameter
        if rmacSL & SL_RMAC:
            assert self.i & M_RMAC, "R-MAC not in SCP parameter"
        if rmacSL & SL_RENC:
            assert self.i & (M_RMACENC ^ M_RMAC), "R-ENC not in SCP parameter"
        # other bits must be zero
        assert rmacSL & ~(SL_RMAC | SL_RENC) == 0, "RFU bits nonzero"
        assert rmacSL & SL_RMAC != 0, "Wrong P1 for BEGIN R-MAC"
        assert self.SL & SL_RENC == 0, "R-ENC already in SL for BEGIN R-MAC"
        assert rmacSL > self.SL & SL_RMAC, \
            "R-MAC already in SL for BEGIN R-MAC"
        assert self.SL & SL_CMAC, "C-MAC was not in Ext Auth"
        assert self.SL & SL_CENC or rmacSL & SL_RENC == 0

        if saltData is not None:
            assert len(saltData) < 255, "Too long data"
            data = chr(len(saltData)) + saltData
        else:
            data = ''
        apdu = [0x80, INS_BEGIN_RMAC, rmacSL, 1, len(data)] + \
               [ord(x) for x in data]
        wapdu = self.wrapAPDU(apdu)
        self.beginRmacSL = rmacSL
        return wapdu

    def deriveKeys(self, card_challenge=None):
        """ Derive session keys and calculate host_ and card_ cryptograms.
card_challenge shall be present if i & M_PSEUDO == 0."""

        if 'host_challenge' not in self.__dict__:
            self.host_challenge = '\0'*8

        # card challenge calculation
        if self.i & M_PSEUDO:
            seqCounter = pack(">L", self.seqCounter)[1:]
            self.card_challenge = KDF(self.keyENC, DDC.CardChallenge, 0x0040,
                                      seqCounter + self.SD_AID)
            if card_challenge is not None:
                assert card_challenge == self.card_challenge, \
                    "Provided and calculated card challenge difer: %s vs. %s" \
                    % (hexlify(card_challenge), hexlify(self.card_challenge))
        else:
            assert len(card_challenge) == 8, \
                "Wrong length of card challenge for randomly generated"
            self.card_challenge = card_challenge

        # session keys derivation
        context = self.host_challenge + self.card_challenge
        self.SENC = KDF(self.keyENC, DDC.S_ENC, 8*len(self.keyENC), context)
        self.SMAC = KDF(self.keyMAC, DDC.S_MAC, 8*len(self.keyMAC), context)
        self.SRMAC = KDF(self.keyMAC, DDC.S_RMAC, 8*len(self.keyMAC),
                         context)

        # cryptograms
        self.card_cryptogram = KDF(self.SMAC, DDC.CardCrypto, 0x0040, context)
        self.host_cryptogram = KDF(self.SMAC, DDC.HostCrypto, 0x0040, context)

        # reset MAC chaining value
        self.MACchain = None

    def CLA(self, zSecure=True, b8=0x80):
        """ Return CLA byte corresponding to logical channel, for
secured/unsecured APDU.
b8 - bit8 of CLA
 """
        if self.logCh < 4:
            return b8 + self.logCh + (zSecure and 0x04 or 0x00)
        else:
            return b8 + 0x40 + (self.logCh - 4) + (zSecure and 0x20 or 0x00)

    def closeSession():
        """ Clear all session data (session keys, logCh, challanges). """
        pass

    def checkAPDU(self, apdu):
        """ Check INS and Lc byte of APDU. Return Lc """
        if apdu[1] == 0xC0:
            assert len(apdu) == 5 and apdu[2:4] == [0, 0], \
                'Wrong Get response APDU'
        else:
            assert apdu[1] & 0xF0 not in (0x60, 0x90), \
                'Wrong INS byte %02X' % apdu[1]
        lc = len(apdu) - 5
        assert len(apdu) >= 5, "Wrong APDU length: %d" % len(apdu)
        assert len(apdu) == 5 or apdu[4] == lc, \
            "Lc differs from length of data: %d vs %d" % (apdu[4], lc)
        return lc

    def wrapAPDU(self, apdu):
        """ Wrap APDU for SCP03, i.e. calculate MAC and encrypt.
Input APDU and output APDU are list of u8. """
        lc = self.checkAPDU(apdu)
        if apdu[1] == 0xC0:  # Get Response TPDU
            return apdu
        if 'beginRmaSL' in self.__dict__:
            self.rmacSL = self.beginRmacSL
            del self.beginRmacSL

        self.cmdCount += 1
        cla = apdu[0]
        b8 = cla & 0x80
        if (cla & 0x40 == 0 and cla & 0x03 > 0) or cla & 0x40 != 0:
            # check logical channels
            assert cla == self.CLA(False, b8), "CLA mismatch"
        scla = b8 | 0x04  # CLA without log. ch. but with secure messaging
        cdata = l2s(apdu[5:])
        if self.SL & SL_CENC and lc > 0:  # C-ENC
            k = AES.new(self.SENC, AES.MODE_ECB)
            ICV = k.encrypt(pack(">QQ", self.cmdCount / 0x10000000000000000L,
                                 self.cmdCount % 0x10000000000000000L))
            k = AES.new(self.SENC, AES.MODE_CBC, IV=ICV)
            data2enc = pad80(cdata, 16)
            cdata = k.encrypt(data2enc)
            lc = len(cdata)
            assert lc <= 0xFF, "Lc after encryption too long: %d" % lc
        if self.SL & SL_CMAC:    # C-MAC
            lc += 8
            assert lc <= 0xFF, "Lc after MACing too long: %d" % lc
            data2sign = self.MACchain + chr(scla) + l2s(apdu[1:4])\
                + chr(lc) + cdata
            self.MACchain = CMAC(self.SMAC, data2sign)
            cdata += self.MACchain[:8]
        apdu = [self.CLA(True, b8)] + apdu[1:4] + [lc] + s2l(cdata)
        return apdu

    def wrapResp(self, resp, sw1, sw2):
        """ Wrap expected response as card would do."""
        sw = (sw1 << 8) + sw2
        if not(sw == 0x9000 or sw1 in (0x62, 0x63)):
            assert len(resp) == 0, "No response data expected"
            return [], sw1, sw2
        dresp = l2s(resp)
        if (self.SL | self.rmacSL) & SL_RENC and len(dresp) > 0:
            assert len(dresp) <= 0xEF, "Data too long for RENC+RMAC"
            k = AES.new(self.SENC, AES.MODE_ECB)
            ICV = k.encrypt(pack(">QQ", 0x8000000000000000L |
                                 self.cmdCount / 0x10000000000000000L,
                                 self.cmdCount % 0x10000000000000000L))
            k = AES.new(self.SENC, AES.MODE_CBC, IV=ICV)
            dresp = k.encrypt(pad80(dresp, 16))
        if (self.SL | self.rmacSL) & SL_RMAC:
            assert len(dresp) <= 0xF0, "Data too long for RMAC"
            data2sign = self.MACchain + dresp + chr(sw1) + chr(sw2)
            rmac = CMAC(self.SRMAC, data2sign)[:8]
            dresp += rmac
        return s2l(dresp), sw1, sw2

    def unwrapAPDU(self, apdu):
        """ Parse MACed/encrypted APDU, decipher and check MAC. """
        lc = self.checkAPDU(apdu)
        if apdu[1] == 0xC0:  # Get Response TPDU
            return apdu
        if 'beginRmaSL' in self.__dict__:
            self.rmacSL = self.beginRmacSL
            del self.beginRmacSL

        self.cmdCount += 1
        cla = apdu[0]
        b8 = cla & 0x80
        assert cla & 0x04, "Secure messaging missing"
        if (cla & 0x40 == 0 and cla & 0x03 > 0) or cla & 0x40 != 0:
            # check logical channels
            assert cla == self.CLA(True, b8), "CLA mismatch"
        scla = b8 | 0x04  # CLA without log. ch. but with secure messaging

        data = l2s(apdu[5:])
        if self.SL & SL_CMAC:    # C-MAC
            assert lc >= 8, "Missing/ too short CMAC"
            sdata = data[:-8]
            data2sign = self.MACchain + chr(scla) + l2s(apdu[1:4])\
                + chr(lc) + sdata
            self.MACchain = CMAC(self.SMAC, data2sign)
            assert data[-8:] == self.MACchain[:8], "Wrong CMAC"
            data = sdata
            lc -= 8
        if self.SL & SL_CENC and lc > 0:  # C-ENC
            assert lc % 16 == 0, "Encoded data length not multiple of BS"
            k = AES.new(self.SENC, AES.MODE_ECB)
            ICV = k.encrypt(pack(">QQ",
                                 self.cmdCount / 0x10000000000000000L,
                                 self.cmdCount % 0x10000000000000000L))
            k = AES.new(self.SENC, AES.MODE_CBC, IV=ICV)
            pdata = k.decrypt(data)
            data = unpad80(pdata, 16)
            assert len(data) > 0, "Empty data encrypted"
            lc = len(data)
        apdu = [self.CLA(False, b8)] + apdu[1:4] + [lc] + s2l(data)
        return apdu

    def unwrapResp(self, resp, sw1, sw2):
        """ Unwrap response (decipher and check MAC)."""
        sw = (sw1 << 8) + sw2
        if not(sw == 0x9000 or sw1 in (0x62, 0x63)):
            assert len(resp) == 0, "No response data expected"
            return [], sw1, sw2
        dresp = l2s(resp)
        if (self.SL | self.rmacSL) & SL_RMAC:
            assert len(resp) >= 8, "Resp data shorter than 8: %d" % len(resp)
            data2sign = self.MACchain + dresp[:-8] + chr(sw1) + chr(sw2)
            rmac = CMAC(self.SRMAC, data2sign)[:8]
            assert rmac == dresp[-8:], "Wrong R-MAC: %s vs expected: %s" % \
                (hexlify(dresp[-8:]).upper(), hexlify(rmac).upper())
            dresp = dresp[:-8]
        if (self.SL | self.rmacSL) & SL_RENC and len(dresp) > 0:
            assert len(dresp) % 16 == 0, \
                "Length of encrypted data not multiple of 16: %d" % len(dresp)
            k = AES.new(self.SENC, AES.MODE_ECB)
            ICV = k.encrypt(pack(">QQ", 0x8000000000000000L |
                                 self.cmdCount / 0x10000000000000000L,
                                 self.cmdCount % 0x10000000000000000L))
            k = AES.new(self.SENC, AES.MODE_CBC, IV=ICV)
            ddata = k.decrypt(dresp)
            data = unpad80(ddata, 16)
            assert len(data) > 0, "Empty data encrypted"
        else:
            data = dresp
        return s2l(data), sw1, sw2

    def getDEK(self):
        return DEK(self.keyDEK)

if 'SCP03Connection' in __all__:
    class ISOerror(Exception):
        def __init__(self, message, sw=None):
            self.message = message
            self.sw = sw

        def __str__(self):
            if self.sw is not None:
                return self.message % self.sw
            else:
                return self.message


    class SCP03Connection(CardConnectionDecorator):
        """ Implements SCP03 as CardConnectionDecorator. """
        def __init__(self, connection, **kw):
            self.scp = SCP03(**kw)
            self.connection = connection
            CardConnectionDecorator.__init__(self, connection)
            if 'GAFdict' in kw:
                assert isinstance(kw['GAFdict'], dict), "GAF dictionary expected"
                self.objects = kw['GAFdict']
            else:
                self.objects = {}

        def mut_auth(self, SL, **kw):
            """ Perform mutual authentication.
Optional paramters in kw:
logCh          - logical channel (u8, default 0)
host_challenge - string 8B long
aid            - AID to select (string, default self.scp)
                 (SD_AID for calculation is self.scp.SD_AID)
 """
            # select SD
            logCh = kw.get('logCh', 0)
            assert 0 <= logCh and logCh < 20
            self.scp.logCh = logCh
            aid = kw.get('aid', self.scp.SD_AID)
            assert 5 <= len(aid) and len(aid) <= 16
            cla = self.scp.CLA(False, b8=0)
            apdu = [cla, 0xA4, 0x04, 0, len(aid)] + s2l(aid)
            resp, sw1, sw2 = CardConnectionDecorator.transmit(self, apdu)
            if sw1 == 0x61:
                apdu = [cla, 0xC0, 0, 0, sw2]
                resp, sw1, sw2 = CardConnectionDecorator.transmit(self, apdu)
            sw = (sw1 << 8) + sw2
            if sw != 0x9000:
                raise ISOerror("NOK: SW = %04X", sw)

            # Initial update
            host_challenge = kw.get('host_challenge', '\0'*8)
            assert len(host_challenge) == 8
            apdu = self.scp.initUpdate(host_challenge, logCh)
            resp, sw1, sw2 = CardConnectionDecorator.transmit(self, apdu)
            # Case 4 command, expects 61xx
            assert sw1 == 0x61
            resp, sw1, sw2 = self.getResponse(sw2)
            sw = (sw1 << 8) + sw2
            if sw != 0x9000:
                raise ISOerror("NOK: SW = %04X", sw)
            # check response to initial update, may raise exception
            self.scp.parseInitUpdateResp(resp)

            # External authenticate
            apdu = self.scp.extAuth(SL)
            resp, sw1, sw2 = CardConnectionDecorator.transmit(self, apdu)
            sw = (sw1 << 8) + sw2
            if sw != 0x9000:
                raise ISOerror("NOK: SW = %04X", sw)

        def transmit(self, apdu, protocol=None):
            """ Wrap APDU and transmit to the card. """
            apdu_w = self.scp.wrapAPDU(apdu)
            resp, sw1, sw2 = CardConnectionDecorator.transmit(self, apdu_w,
                                                              protocol)
            while sw1 == 0x61:
                resp1, sw1, sw2 = self.getResponse(sw2)
                resp += resp1
            resp, sw1, sw2 = self.scp.unwrapResp(resp, sw1, sw2)
            return resp, sw1, sw2

        def getResponse(self, sw2):
            """ Get Response from the last APDU."""
            apdu = [self.scp.CLA(False, b8=0), 0xC0, 0x00, 0x00, sw2]
            return CardConnectionDecorator.transmit(self, apdu)

        def send(self, templ, **kw):
            """ Evaluate GAF and transmit as APDU
templ   - a GAF template to evaluate
kw      - GAF dictionary (updates dictionary from SCP03Connection.__init__)
Return (resp, SW) as (str, int)"""
            objects = self.objects.copy()
            objects.update(kw)
            papdu = s2l(GAF(templ).eval(**objects))
            apdu = self.scp.wrapAPDU(papdu)
            resp, sw1, sw2 = CardConnectionDecorator.transmit(self, apdu)
            if sw1 == 0x6C and len(papdu) == 5:
                papdu[4] = sw2
                apdu = self.scp.wrapAPDU(papdu)
                resp, sw1, sw2 = CardConnectionDecorator.transmit(self, apdu)
            else:
                while sw1 == 0x61:
                    resp1, sw1, sw2 = self.getResponse(sw2)
                    resp += resp1
            resp, sw1, sw2 = self.scp.unwrapResp(resp, sw1, sw2)
            sw = (sw1 << 8) + sw2
            return l2s(resp), sw

        def getDEK(self):
            return DEK(self.scp.keyDEK)


# Unitary tests
class Test128(unittest.TestCase):
    def setUp(self):
        self.scp_par = {
            'SD_AID': unhexlify('A000000018434D08090A0B0C000000'),
            'keyENC': '@ABCDEFGHIJKLMNO',
            'keyMAC': unhexlify('4011223344455667') + 'HIJKLMNO',
            'keyDEK': unhexlify('9876543210') + '@ABCDEFGHIJ',
            'keyVer': 0x30,
            'seqCounter': 0x00002A,
            'diverData': unhexlify('000050C7606A8CF64800'), }

    def test_Mutauth(self):
        host_challenge = unhexlify('0807060504030201')
        scp = SCP03(**self.scp_par)
        apdu = scp.initUpdate(host_challenge)
        self.assertEqual(l2s(apdu),
                         unhexlify('80503000080807060504030201'))
        # invoke key derivation
        resp = scp.initUpdateResp()
        self.assertEqual(scp.card_challenge,
                         unhexlify('A3F5F144D19BE66E'))
        self.assertEqual(scp.SENC,
                         unhexlify('852D207B7CC8C880231EDFD5C644CFB1'))
        self.assertEqual(scp.SMAC,
                         unhexlify('7131B9369F3D19850E6919CD3321523E'))
        self.assertEqual(scp.SRMAC,
                         unhexlify('B570AA1FDE18F9179B5CBD42D8939D05'))
        self.assertEqual(scp.card_cryptogram,
                         unhexlify('72BFCBDF4A14515F'))
        self.assertEqual(scp.host_cryptogram,
                         unhexlify('AEB8DAD1865B85E2'))
        self.assertEqual(resp,
                         unhexlify('000050C7606A8CF64800300370' +
                                   'A3F5F144D19BE66E72BFCBDF4A14515F00002A'))
        # external authenticate
        apdu = scp.extAuth(SL=1)
        self.assertEqual(l2s(apdu),
                         unhexlify('8482010010AEB8DAD1865B85E2') +
                         unhexlify('49FC4CF184E61DCD'))
        self.assertEqual(scp.MACchain,
                         unhexlify('49FC4CF184E61DCD4C3928E4C617FBA3'))

    def test_Cdecrypt(self):
        host_challenge = unhexlify('0807060504030201')
        scp = SCP03(**self.scp_par)
        apdu = scp.initUpdate(host_challenge)
        apdu = scp.extAuth(SL=3)

        apdu_i4lh = '80E60200150A45786572636973655236000006EF04C602068200'
        apdu_i4l = s2l(unhexlify(apdu_i4lh))
        wapdu = scp.wrapAPDU(apdu_i4l)
        encdata = l2s(wapdu[5:-8])
        self.assertEqual(encdata,
                         unhexlify('DF31907FC027482D5DCB7DC028245F7C108CA4D2AFF12275079768E1EFE9429E'))

    def unwrapAPDU(self, SL):
        host_challenge = unhexlify('0807060504030201')
        scp = SCP03(**self.scp_par)
        apdu = scp.initUpdate(host_challenge)
        apdu = scp.extAuth(SL)
        macchain = scp.MACchain
        cmdCount = scp.cmdCount

        apdu_i4lh = '80E60200150A45786572636973655236000006EF04C602068200'
        apdu_i4l = s2l(unhexlify(apdu_i4lh))
        wapdu = scp.wrapAPDU(apdu_i4l)

        scp.MACchain = macchain
        scp.cmdCount = cmdCount
        papdu = scp.unwrapAPDU(wapdu)
        self.assertEqual(papdu, apdu_i4l)

    def test_unwrapAPDU_01(self):
        self.unwrapAPDU(01)

    def test_unwrapAPDU_03(self):
        self.unwrapAPDU(03)

    def test_beginRMAC(self):
        host_challenge = unhexlify('0807060504030201')
        scp = SCP03(**self.scp_par)
        apdu = scp.initUpdate(host_challenge)
        apdu = scp.extAuth(SL=1)

        wapdu = scp.beginRMAC(SL_RMAC)

if __name__ == '__main__':
    unittest.main()
