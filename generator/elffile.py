import bisect
from weakref import WeakKeyDictionary

from elftools.elf.constants import SH_FLAGS
from elftools.elf.enums import ENUM_SH_TYPE, ENUM_NOTE_N_TYPE

from consts import *
from util import memoize

set_const_raw(SH_FLAGS.__dict__)
set_const_str(ENUM_SH_TYPE)
set_const_str(ENUM_NOTE_N_TYPE)

def get_build_id(elf):
	section_name = '.note.gnu.build-id'
	n_type = STR.NT_GNU_BUILD_ID

	sec = elf.get_section_by_name(section_name)
	if sec is None:
		return
	
	for note in sec.iter_notes():
		if note['n_type'] == n_type:
			return note['n_desc']

	print ("ELF section %s doesn't have %s descriptor" %
			(section, n_type))

class AddressSpace(object):
	def __init__(self, elf):
		# See comment from get_dio_by_pos() on how lookup is done
		self._sec_info = sec_info = [(-sec.header.sh_addr, sec)
			for sec in elf.iter_sections()
				if sec.header.sh_flags & RAW.SHF_ALLOC]

		sec_info.append((1, None))
		sec_info.sort()

	def get_section(self, addr):
		key = (-addr,)
		idx = bisect.bisect(self._sec_info, key)
		_, sec = self._sec_info[idx]
		if not sec:
			return

		if addr - sec.header.sh_addr >= sec.header.sh_size:
			return
		return sec

get_address_space = memoize(WeakKeyDictionary)(AddressSpace)

class MemoryStream(object):
	def __init__(self, elf):
		self.elf = elf
		self._address_space = get_address_space(elf)

	def seek(self, addr):
		self._addr = addr

	def tell(self):
		return self._addr

	def _read(self, size):
		assert size >= 0
		if not size:
			return ""

		get_section = self._address_space.get_section

		addr = self._addr
		next_addr = addr + size

		sec = get_section(addr)
		if not sec:
			raise Exception("No section for address {:x}".format(addr))

		next_sec = get_section(next_addr - 1)
		# Make sure one read() call processes data from one section
		if sec is not next_sec:
			next_addr = sec.header.sh_addr + sec.header.sh_size
			size = next_addr - addr

		if sec.header.sh_type == STR.SHT_NOBITS:
			data = "\x00" * size
		else:
			stream = sec.stream
			offset = addr - sec.header.sh_addr + sec.header.sh_offset
			stream.seek(offset)
			data = stream.read(size)
			if len(data) != size:
				raise Exception("Short read: got {}, expected {}".format(
							len(data), size))

		self._addr = next_addr
		return data

	def read(self, size, allow_short=False):
		data = self._read(size)
		assert len(data) <= size
		if not allow_short and len(data) < size:
			raise Exception("Short read: got {}, expected {}".format(
				len(data), size))
		return data

