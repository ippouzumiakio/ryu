# Copyright (C) 2015 Nippon Telegraph and Telephone Corporation.
# Copyright (C) 2015 YAMAMOTO Takashi <yamamoto at valinux co jp>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import six

import struct

from ryu import utils
from ryu.lib import type_desc
from ryu.ofproto import nicira_ext
from ryu.ofproto import ofproto_common
from ryu.lib.pack_utils import msg_pack_into
from ryu.ofproto.ofproto_parser import StringifyMixin


def generate(ofp_name, ofpp_name):
    import sys

    ofp = sys.modules[ofp_name]
    ofpp = sys.modules[ofpp_name]

    class _NXFlowSpec(StringifyMixin):
        _hdr_fmt_str = '!H'  # 2 bit 0s, 1 bit src, 2 bit dst, 11 bit n_bits
        _dst_type = None
        _subclasses = {}
        _TYPE = {
            'nx-flow-spec-field': [
                'src',
                'dst',
            ]
        }

        def __init__(self, src, dst, n_bits):
            self.src = src
            self.dst = dst
            self.n_bits = n_bits

        @classmethod
        def register(cls, subcls):
            assert issubclass(subcls, cls)
            assert subcls._dst_type not in cls._subclasses
            cls._subclasses[subcls._dst_type] = subcls

        @classmethod
        def parse(cls, buf):
            (hdr,) = struct.unpack_from(cls._hdr_fmt_str, buf, 0)
            rest = buf[struct.calcsize(cls._hdr_fmt_str):]
            if hdr == 0:
                return None, rest  # all-0 header is no-op for padding
            src_type = (hdr >> 13) & 0x1
            dst_type = (hdr >> 11) & 0x3
            n_bits = hdr & 0x3ff
            subcls = cls._subclasses[dst_type]
            if src_type == 0:  # subfield
                src = cls._parse_subfield(rest)
                rest = rest[6:]
            elif src_type == 1:  # immediate
                src_len = (n_bits + 15) // 16 * 2
                src_bin = rest[:src_len]
                src = type_desc.IntDescr(size=src_len).to_user(src_bin)
                rest = rest[src_len:]
            if dst_type == 0:  # match
                dst = cls._parse_subfield(rest)
                rest = rest[6:]
            elif dst_type == 1:  # load
                dst = cls._parse_subfield(rest)
                rest = rest[6:]
            elif dst_type == 2:  # output
                dst = ''  # empty
            return subcls(src=src, dst=dst, n_bits=n_bits), rest

        def serialize(self):
            buf = bytearray()
            if isinstance(self.src, tuple):
                src_type = 0  # subfield
            else:
                src_type = 1  # immediate
            # header
            val = (src_type << 13) | (self._dst_type << 11) | self.n_bits
            msg_pack_into(self._hdr_fmt_str, buf, 0, val)
            # src
            if src_type == 0:  # subfield
                buf += self._serialize_subfield(self.src)
            elif src_type == 1:  # immediate
                src_len = (self.n_bits + 15) // 16 * 2
                buf += type_desc.IntDescr(size=src_len).from_user(self.src)
            # dst
            if self._dst_type == 0:  # match
                buf += self._serialize_subfield(self.dst)
            elif self._dst_type == 1:  # load
                buf += self._serialize_subfield(self.dst)
            elif self._dst_type == 2:  # output
                pass  # empty
            return buf

        @staticmethod
        def _parse_subfield(buf):
            (n, len) = ofp.oxm_parse_header(buf, 0)
            assert len == 4  # only 4-bytes NXM/OXM are defined
            field = ofp.oxm_to_user_header(n)
            rest = buf[len:]
            (ofs,) = struct.unpack_from('!H', rest, 0)
            return (field, ofs)

        @staticmethod
        def _serialize_subfield(subfield):
            (field, ofs) = subfield
            buf = bytearray()
            n = ofp.oxm_from_user_header(field)
            ofp.oxm_serialize_header(n, buf, 0)
            assert len(buf) == 4  # only 4-bytes NXM/OXM are defined
            msg_pack_into('!H', buf, 4, ofs)
            return buf

    class NXFlowSpecMatch(_NXFlowSpec):
        # Add a match criteria
        # an example of the corresponding ovs-ofctl syntax:
        #    NXM_OF_VLAN_TCI[0..11]
        _dst_type = 0

    class NXFlowSpecLoad(_NXFlowSpec):
        # Add NXAST_REG_LOAD actions
        # an example of the corresponding ovs-ofctl syntax:
        #    NXM_OF_ETH_DST[]=NXM_OF_ETH_SRC[]
        _dst_type = 1

    class NXFlowSpecOutput(_NXFlowSpec):
        # Add an OFPAT_OUTPUT action
        # an example of the corresponding ovs-ofctl syntax:
        #    output:NXM_OF_IN_PORT[]
        _dst_type = 2

        def __init__(self, src, n_bits, dst=''):
            assert dst == ''
            super(NXFlowSpecOutput, self).__init__(src=src, dst=dst,
                                                   n_bits=n_bits)

    class NXAction(ofpp.OFPActionExperimenter):
        _fmt_str = '!H'  # subtype
        _subtypes = {}
        _experimenter = ofproto_common.NX_EXPERIMENTER_ID

        def __init__(self):
            super(NXAction, self).__init__(self._experimenter)
            self.subtype = self._subtype

        @classmethod
        def parse(cls, buf):
            fmt_str = NXAction._fmt_str
            (subtype,) = struct.unpack_from(fmt_str, buf, 0)
            subtype_cls = cls._subtypes.get(subtype)
            rest = buf[struct.calcsize(fmt_str):]
            if subtype_cls is None:
                return NXActionUnknown(subtype, rest)
            return subtype_cls.parser(rest)

        def serialize(self, buf, offset):
            data = self.serialize_body()
            payload_offset = (
                ofp.OFP_ACTION_EXPERIMENTER_HEADER_SIZE +
                struct.calcsize(NXAction._fmt_str)
            )
            self.len = utils.round_up(payload_offset + len(data), 8)
            super(NXAction, self).serialize(buf, offset)
            msg_pack_into(NXAction._fmt_str,
                          buf,
                          offset + ofp.OFP_ACTION_EXPERIMENTER_HEADER_SIZE,
                          self.subtype)
            buf += data

        @classmethod
        def register(cls, subtype_cls):
            assert subtype_cls._subtype is not cls._subtypes
            cls._subtypes[subtype_cls._subtype] = subtype_cls

    class NXActionUnknown(NXAction):
        def __init__(self, subtype, data=None,
                     type_=None, len_=None, experimenter=None):
            self._subtype = subtype
            super(NXActionUnknown, self).__init__()
            self.data = data

        @classmethod
        def parser(cls, buf):
            return cls(data=buf)

        def serialize_body(self):
            # fixup
            return bytearray() if self.data is None else self.data

    class NXActionPopQueue(NXAction):
        _subtype = nicira_ext.NXAST_POP_QUEUE

        _fmt_str = '!6x'

        def __init__(self,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionPopQueue, self).__init__()

        @classmethod
        def parser(cls, buf):
            return cls()

        def serialize_body(self):
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0)
            return data

    class NXActionRegLoad(NXAction):
        _subtype = nicira_ext.NXAST_REG_LOAD
        _fmt_str = '!HIQ'  # ofs_nbits, dst, value
        _TYPE = {
            'ascii': [
                'dst',
            ]
        }

        def __init__(self, start, end, dst, value,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionRegLoad, self).__init__()
            self.start = start
            self.end = end
            self.dst = dst
            self.value = value

        @classmethod
        def parser(cls, buf):
            (ofs_nbits, dst, value,) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            start = ofs_nbits >> 6
            end = (ofs_nbits & 0x3f) + start
            # Right-shift instead of using oxm_parse_header for simplicity...
            dst_name = ofp.oxm_to_user_header(dst >> 9)
            return cls(start, end, dst_name, value)

        def serialize_body(self):
            hdr_data = bytearray()
            n = ofp.oxm_from_user_header(self.dst)
            ofp.oxm_serialize_header(n, hdr_data, 0)
            (dst_num,) = struct.unpack_from('!I', six.binary_type(hdr_data), 0)

            ofs_nbits = (self.start << 6) + (self.end - self.start)
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          ofs_nbits, dst_num, self.value)
            return data

    class NXActionNote(NXAction):
        _subtype = nicira_ext.NXAST_NOTE

        # note
        _fmt_str = '!%dB'

        # set the integer array in a note
        def __init__(self,
                     note,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionNote, self).__init__()
            self.note = note

        @classmethod
        def parser(cls, buf):
            note = struct.unpack_from(
                cls._fmt_str % len(buf), buf, 0)
            return cls(list(note))

        def serialize_body(self):
            assert isinstance(self.note, (tuple, list))
            for n in self.note:
                assert isinstance(n, six.integer_types)

            pad = (len(self.note) + nicira_ext.NX_ACTION_HEADER_0_SIZE) % 8
            if pad:
                self.note += [0x0 for i in range(8 - pad)]
            note_len = len(self.note)
            data = bytearray()
            msg_pack_into(self._fmt_str % note_len, data, 0,
                          *self.note)
            return data

    class _NXActionSetTunnelBase(NXAction):
        # _subtype, _fmt_str must be attributes of subclass.

        def __init__(self,
                     tun_id,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(_NXActionSetTunnelBase, self).__init__()
            self.tun_id = tun_id

        @classmethod
        def parser(cls, buf):
            (tun_id,) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            return cls(tun_id)

        def serialize_body(self):
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.tun_id)
            return data

    class NXActionSetTunnel(_NXActionSetTunnelBase):
        _subtype = nicira_ext.NXAST_SET_TUNNEL

        # tun_id
        _fmt_str = '!2xI'

    class NXActionSetTunnel64(_NXActionSetTunnelBase):
        _subtype = nicira_ext.NXAST_SET_TUNNEL64

        # tun_id
        _fmt_str = '!6xQ'

    class NXActionRegMove(NXAction):
        _subtype = nicira_ext.NXAST_REG_MOVE
        _fmt_str = '!HHH'  # n_bits, src_ofs, dst_ofs
        # Followed by OXM fields (src, dst) and padding to 8 bytes boundary
        _TYPE = {
            'ascii': [
                'src_field',
                'dst_field',
            ]
        }

        def __init__(self, src_field, dst_field, n_bits, src_ofs=0, dst_ofs=0,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionRegMove, self).__init__()
            self.n_bits = n_bits
            self.src_ofs = src_ofs
            self.dst_ofs = dst_ofs
            self.src_field = src_field
            self.dst_field = dst_field

        @classmethod
        def parser(cls, buf):
            (n_bits, src_ofs, dst_ofs,) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            rest = buf[struct.calcsize(NXActionRegMove._fmt_str):]
            # src field
            (n, len) = ofp.oxm_parse_header(rest, 0)
            src_field = ofp.oxm_to_user_header(n)
            rest = rest[len:]
            # dst field
            (n, len) = ofp.oxm_parse_header(rest, 0)
            dst_field = ofp.oxm_to_user_header(n)
            rest = rest[len:]
            # ignore padding
            return cls(src_field, dst_field=dst_field, n_bits=n_bits,
                       src_ofs=src_ofs, dst_ofs=dst_ofs)

        def serialize_body(self):
            # fixup
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.n_bits, self.src_ofs, self.dst_ofs)
            # src field
            n = ofp.oxm_from_user_header(self.src_field)
            ofp.oxm_serialize_header(n, data, len(data))
            # dst field
            n = ofp.oxm_from_user_header(self.dst_field)
            ofp.oxm_serialize_header(n, data, len(data))
            return data

    class NXActionResubmit(NXAction):
        _subtype = nicira_ext.NXAST_RESUBMIT

        # in_port
        _fmt_str = '!H4x'

        def __init__(self,
                     in_port=0xfff8,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionResubmit, self).__init__()
            self.in_port = in_port

        @classmethod
        def parser(cls, buf):
            (in_port,) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            return cls(in_port)

        def serialize_body(self):
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.in_port)
            return data

    class NXActionResubmitTable(NXAction):
        _subtype = nicira_ext.NXAST_RESUBMIT_TABLE

        # in_port, table_id
        _fmt_str = '!HB3x'

        def __init__(self,
                     in_port=0xfff8,
                     table_id=0xff,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionResubmitTable, self).__init__()
            self.in_port = in_port
            self.table_id = table_id

        @classmethod
        def parser(cls, buf):
            (in_port,
             table_id) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            return cls(in_port, table_id)

        def serialize_body(self):
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.in_port, self.table_id)
            return data

    class NXActionOutputReg(NXAction):
        _subtype = nicira_ext.NXAST_OUTPUT_REG

        # ofs_nbits, src, max_len
        _fmt_str = '!HIH6x'

        def __init__(self,
                     start,
                     end,
                     src,
                     max_len,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionOutputReg, self).__init__()
            self.start = start
            self.end = end
            self.src = src
            self.max_len = max_len

        @classmethod
        def parser(cls, buf):
            (ofs_nbits,
             src,
             max_len) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            start = ofs_nbits >> 6
            end = (ofs_nbits & 0x3f) + start
            return cls(start,
                       end,
                       src,
                       max_len)

        def serialize_body(self):
            data = bytearray()
            ofs_nbits = (self.start << 6) + (self.end - self.start)
            msg_pack_into(self._fmt_str, data, 0,
                          ofs_nbits,
                          self.src,
                          self.max_len)
            return data

    class NXActionLearn(NXAction):
        _subtype = nicira_ext.NXAST_LEARN

        # idle_timeout, hard_timeout, priority, cookie, flags,
        # table_id, pad, fin_idle_timeout, fin_hard_timeout
        _fmt_str = '!HHHQHBxHH'
        # Followed by flow_mod_specs

        def __init__(self,
                     table_id,
                     specs,
                     idle_timeout=0,
                     hard_timeout=0,
                     priority=ofp.OFP_DEFAULT_PRIORITY,
                     cookie=0,
                     flags=0,
                     fin_idle_timeout=0,
                     fin_hard_timeout=0,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionLearn, self).__init__()
            self.idle_timeout = idle_timeout
            self.hard_timeout = hard_timeout
            self.priority = priority
            self.cookie = cookie
            self.flags = flags
            self.table_id = table_id
            self.fin_idle_timeout = fin_idle_timeout
            self.fin_hard_timeout = fin_hard_timeout
            self.specs = specs

        @classmethod
        def parser(cls, buf):
            (idle_timeout,
             hard_timeout,
             priority,
             cookie,
             flags,
             table_id,
             fin_idle_timeout,
             fin_hard_timeout,) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            rest = buf[struct.calcsize(cls._fmt_str):]
            # specs
            specs = []
            while len(rest) > 0:
                spec, rest = _NXFlowSpec.parse(rest)
                if spec is None:
                    continue
                specs.append(spec)
            return cls(idle_timeout=idle_timeout,
                       hard_timeout=hard_timeout,
                       priority=priority,
                       cookie=cookie,
                       flags=flags,
                       table_id=table_id,
                       fin_idle_timeout=fin_idle_timeout,
                       fin_hard_timeout=fin_hard_timeout,
                       specs=specs)

        def serialize_body(self):
            # fixup
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.idle_timeout,
                          self.hard_timeout,
                          self.priority,
                          self.cookie,
                          self.flags,
                          self.table_id,
                          self.fin_idle_timeout,
                          self.fin_hard_timeout)
            for spec in self.specs:
                data += spec.serialize()
            return data

    class NXActionExit(NXAction):
        _subtype = nicira_ext.NXAST_EXIT

        _fmt_str = '!6x'

        def __init__(self,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionExit, self).__init__()

        @classmethod
        def parser(cls, buf):
            return cls()

        def serialize_body(self):
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0)
            return data

    class NXActionController(NXAction):
        _subtype = nicira_ext.NXAST_CONTROLLER

        # max_len, controller_id, reason
        _fmt_str = '!HHBx'

        def __init__(self,
                     max_len,
                     controller_id,
                     reason,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionController, self).__init__()
            self.max_len = max_len
            self.controller_id = controller_id
            self.reason = reason

        @classmethod
        def parser(cls, buf):
            (max_len,
             controller_id,
             reason) = struct.unpack_from(
                cls._fmt_str, buf)
            return cls(max_len,
                       controller_id,
                       reason)

        def serialize_body(self):
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.max_len,
                          self.controller_id,
                          self.reason)
            return data

    class NXActionFinTimeout(NXAction):
        _subtype = nicira_ext.NXAST_FIN_TIMEOUT

        # fin_idle_timeout, fin_hard_timeout
        _fmt_str = '!HH2x'

        def __init__(self,
                     fin_idle_timeout,
                     fin_hard_timeout,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionFinTimeout, self).__init__()
            self.fin_idle_timeout = fin_idle_timeout
            self.fin_hard_timeout = fin_hard_timeout

        @classmethod
        def parser(cls, buf):
            (fin_idle_timeout,
             fin_hard_timeout) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            return cls(fin_idle_timeout,
                       fin_hard_timeout)

        def serialize_body(self):
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.fin_idle_timeout,
                          self.fin_hard_timeout)
            return data

    class NXActionConjunction(NXAction):
        _subtype = nicira_ext.NXAST_CONJUNCTION

        # clause, n_clauses, id
        _fmt_str = '!BBI'

        def __init__(self,
                     clause,
                     n_clauses,
                     id_,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionConjunction, self).__init__()
            self.clause = clause
            self.n_clauses = n_clauses
            self.id = id_

        @classmethod
        def parser(cls, buf):
            (clause,
             n_clauses,
             id_,) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            return cls(clause, n_clauses, id_)

        def serialize_body(self):
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.clause,
                          self.n_clauses,
                          self.id)
            return data

    class NXActionMultipath(NXAction):
        _subtype = nicira_ext.NXAST_MULTIPATH

        # fields, basis, algorithm, max_link,
        # arg, ofs_nbits, dst
        _fmt_str = '!HH2xHHI2xHI'

        def __init__(self,
                     fields,
                     basis,
                     algorithm,
                     max_link,
                     arg,
                     start,
                     end,
                     dst,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionMultipath, self).__init__()
            self.fields = fields
            self.basis = basis
            self.algorithm = algorithm
            self.max_link = max_link
            self.arg = arg
            self.start = start
            self.end = end
            self.dst = dst

        @classmethod
        def parser(cls, buf):
            (fields,
             basis,
             algorithm,
             max_link,
             arg,
             ofs_nbits,
             dst) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            start = ofs_nbits >> 6
            end = (ofs_nbits & 0x3f) + start
            return cls(fields,
                       basis,
                       algorithm,
                       max_link,
                       arg,
                       start,
                       end,
                       dst)

        def serialize_body(self):
            ofs_nbits = (self.start << 6) + (self.end - self.start)
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.fields,
                          self.basis,
                          self.algorithm,
                          self.max_link,
                          self.arg,
                          ofs_nbits,
                          self.dst)
            return data

    class _NXActionBundleBase(NXAction):
        # algorithm, fields, basis, slave_type, n_slaves
        # ofs_nbits, dst, slaves
        _fmt_str = '!HHHIHHI4x'

        def __init__(self, algorithm, fields, basis, slave_type, n_slaves,
                     start, end, dst, slaves):
            super(_NXActionBundleBase, self).__init__()
            self.len = utils.round_up(
                nicira_ext.NX_ACTION_BUNDLE_0_SIZE + len(slaves) * 2, 8)

            self.algorithm = algorithm
            self.fields = fields
            self.basis = basis
            self.slave_type = slave_type
            self.n_slaves = n_slaves
            self.start = start
            self.end = end
            self.dst = dst

            assert isinstance(slaves, (list, tuple))
            for s in slaves:
                assert isinstance(s, six.integer_types)

            self.slaves = slaves

        @classmethod
        def parser(cls, buf):
            (algorithm, fields, basis,
                slave_type, n_slaves, ofs_nbits, dst) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            start = ofs_nbits >> 6
            end = (ofs_nbits & 0x3f) + start
            slave_offset = (nicira_ext.NX_ACTION_BUNDLE_0_SIZE -
                            nicira_ext.NX_ACTION_HEADER_0_SIZE)

            slaves = []
            for i in range(0, n_slaves):
                s = struct.unpack_from('!H', buf, slave_offset)
                slaves.append(s[0])
                slave_offset += 2

            return cls(algorithm, fields, basis, slave_type,
                       n_slaves, start, end, dst, slaves)

        def serialize_body(self):
            ofs_nbits = (self.start << 6) + (self.end - self.start)
            data = bytearray()
            slave_offset = (nicira_ext.NX_ACTION_BUNDLE_0_SIZE -
                            nicira_ext.NX_ACTION_HEADER_0_SIZE)
            self.n_slaves = len(self.slaves)
            for s in self.slaves:
                msg_pack_into('!H', data, slave_offset, s)
                slave_offset += 2
            pad_len = (utils.round_up(self.n_slaves, 4) -
                       self.n_slaves)

            if pad_len != 0:
                msg_pack_into('%dx' % pad_len * 2, data, slave_offset)

            msg_pack_into(self._fmt_str, data, 0,
                          self.algorithm, self.fields, self.basis,
                          self.slave_type, self.n_slaves,
                          ofs_nbits, self.dst)

            return data

    class NXActionBundle(_NXActionBundleBase):
        _subtype = nicira_ext.NXAST_BUNDLE

        def __init__(self, algorithm, fields, basis, slave_type, n_slaves,
                     start, end, dst, slaves):
            # NXAST_BUNDLE actions should have 'ofs_nbits' and 'dst' zeroed.
            super(NXActionBundle, self).__init__(
                algorithm, fields, basis, slave_type, n_slaves,
                start=0, end=0, dst=0, slaves=slaves)

    class NXActionBundleLoad(_NXActionBundleBase):
        _subtype = nicira_ext.NXAST_BUNDLE_LOAD

        def __init__(self, algorithm, fields, basis, slave_type, n_slaves,
                     start, end, dst, slaves):
            super(NXActionBundleLoad, self).__init__(
                algorithm, fields, basis, slave_type, n_slaves,
                start, end, dst, slaves)

    class NXActionCT(NXAction):
        _subtype = nicira_ext.NXAST_CT

        # flags, zone_src, zone_ofs_nbits, recirc_table,
        # pad, alg
        _fmt_str = '!HIHB3xH'
        # Followed by actions

        def __init__(self,
                     flags,
                     zone_src,
                     zone_start,
                     zone_end,
                     recirc_table,
                     alg,
                     actions,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionCT, self).__init__()
            self.flags = flags
            self.zone_src = zone_src
            self.zone_start = zone_start
            self.zone_end = zone_end
            self.recirc_table = recirc_table
            self.alg = alg
            self.actions = actions

        @classmethod
        def parser(cls, buf):
            (flags,
             zone_src,
             zone_ofs_nbits,
             recirc_table,
             alg,) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            zone_start = zone_ofs_nbits >> 6
            zone_end = (zone_ofs_nbits & 0x3f) + zone_start
            rest = buf[struct.calcsize(cls._fmt_str):]
            # actions
            actions = []
            while len(rest) > 0:
                action = ofpp.OFPAction.parser(rest, 0)
                actions.append(action)
                rest = rest[action.len:]

            return cls(flags, zone_src, zone_start, zone_end, recirc_table,
                       alg, actions)

        def serialize_body(self):
            zone_ofs_nbits = ((self.zone_start << 6) +
                              (self.zone_end - self.zone_start))
            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.flags,
                          self.zone_src,
                          zone_ofs_nbits,
                          self.recirc_table,
                          self.alg)
            for a in self.actions:
                a.serialize(data, len(data))
            return data

    class NXActionNAT(NXAction):
        _subtype = nicira_ext.NXAST_NAT

        # pad, flags, range_present
        _fmt_str = '!2xHH'
        # Followed by optional parameters

        _TYPE = {
            'ascii': [
                'range_ipv4_max',
                'range_ipv4_min',
                'range_ipv6_max',
                'range_ipv6_min',
            ]
        }

        def __init__(self,
                     flags,
                     range_ipv4_min='',
                     range_ipv4_max='',
                     range_ipv6_min='',
                     range_ipv6_max='',
                     range_proto_min=None,
                     range_proto_max=None,
                     type_=None, len_=None, experimenter=None, subtype=None):
            super(NXActionNAT, self).__init__()
            self.flags = flags
            self.range_ipv4_min = range_ipv4_min
            self.range_ipv4_max = range_ipv4_max
            self.range_ipv6_min = range_ipv6_min
            self.range_ipv6_max = range_ipv6_max
            self.range_proto_min = range_proto_min
            self.range_proto_max = range_proto_max

        @classmethod
        def parser(cls, buf):
            (flags,
             range_present) = struct.unpack_from(
                cls._fmt_str, buf, 0)
            rest = buf[struct.calcsize(cls._fmt_str):]
            # optional parameters
            kwargs = dict()
            if range_present & nicira_ext.NX_NAT_RANGE_IPV4_MIN:
                kwargs['range_ipv4_min'] = type_desc.IPv4Addr.to_user(rest[:4])
                rest = rest[4:]
            if range_present & nicira_ext.NX_NAT_RANGE_IPV4_MAX:
                kwargs['range_ipv4_max'] = type_desc.IPv4Addr.to_user(rest[:4])
                rest = rest[4:]
            if range_present & nicira_ext.NX_NAT_RANGE_IPV6_MIN:
                kwargs['range_ipv6_min'] = (
                    type_desc.IPv6Addr.to_user(rest[:16]))
                rest = rest[16:]
            if range_present & nicira_ext.NX_NAT_RANGE_IPV6_MAX:
                kwargs['range_ipv6_max'] = (
                    type_desc.IPv6Addr.to_user(rest[:16]))
                rest = rest[16:]
            if range_present & nicira_ext.NX_NAT_RANGE_PROTO_MIN:
                kwargs['range_proto_min'] = type_desc.Int2.to_user(rest[:2])
                rest = rest[2:]
            if range_present & nicira_ext.NX_NAT_RANGE_PROTO_MAX:
                kwargs['range_proto_max'] = type_desc.Int2.to_user(rest[:2])

            return cls(flags, **kwargs)

        def serialize_body(self):
            # Pack optional parameters first, as range_present needs
            # to be calculated.
            optional_data = b''
            range_present = 0
            if self.range_ipv4_min != '':
                range_present |= nicira_ext.NX_NAT_RANGE_IPV4_MIN
                optional_data += type_desc.IPv4Addr.from_user(
                    self.range_ipv4_min)
            if self.range_ipv4_max != '':
                range_present |= nicira_ext.NX_NAT_RANGE_IPV4_MAX
                optional_data += type_desc.IPv4Addr.from_user(
                    self.range_ipv4_max)
            if self.range_ipv6_min != '':
                range_present |= nicira_ext.NX_NAT_RANGE_IPV6_MIN
                optional_data += type_desc.IPv6Addr.from_user(
                    self.range_ipv6_min)
            if self.range_ipv6_max != '':
                range_present |= nicira_ext.NX_NAT_RANGE_IPV6_MAX
                optional_data += type_desc.IPv6Addr.from_user(
                    self.range_ipv6_max)
            if self.range_proto_min is not None:
                range_present |= nicira_ext.NX_NAT_RANGE_PROTO_MIN
                optional_data += type_desc.Int2.from_user(
                    self.range_proto_min)
            if self.range_proto_max is not None:
                range_present |= nicira_ext.NX_NAT_RANGE_PROTO_MAX
                optional_data += type_desc.Int2.from_user(
                    self.range_proto_max)

            data = bytearray()
            msg_pack_into(self._fmt_str, data, 0,
                          self.flags,
                          range_present)
            msg_pack_into('!%ds' % len(optional_data), data, len(data),
                          optional_data)

            return data

    def add_attr(k, v):
        v.__module__ = ofpp.__name__  # Necessary for stringify stuff
        setattr(ofpp, k, v)

    add_attr('NXAction', NXAction)
    add_attr('NXActionUnknown', NXActionUnknown)

    classes = [
        'NXActionPopQueue',
        'NXActionRegLoad',
        'NXActionNote',
        'NXActionSetTunnel',
        'NXActionSetTunnel64',
        'NXActionRegMove',
        'NXActionResubmit',
        'NXActionResubmitTable',
        'NXActionOutputReg',
        'NXActionLearn',
        'NXActionExit',
        'NXActionController',
        'NXActionFinTimeout',
        'NXActionConjunction',
        'NXActionMultipath',
        'NXActionBundle',
        'NXActionBundleLoad',
        'NXActionCT',
        'NXActionNAT',
        '_NXFlowSpec',  # exported for testing
        'NXFlowSpecMatch',
        'NXFlowSpecLoad',
        'NXFlowSpecOutput',
    ]
    vars = locals()
    for name in classes:
        cls = vars[name]
        add_attr(name, cls)
        if issubclass(cls, NXAction):
            NXAction.register(cls)
        if issubclass(cls, _NXFlowSpec):
            _NXFlowSpec.register(cls)
