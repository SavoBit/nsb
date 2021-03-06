from __future__ import print_function

import array
import bisect
from weakref import WeakKeyDictionary
import itertools

from elftools.dwarf import enums, dwarf_expr, descriptions
from elftools.dwarf import constants as dwarf_const
from elftools.dwarf.die import DIE

from consts import *
from util import memoize, rtoi
from elffile import MemoryStream

set_const_str(enums.ENUM_DW_TAG)
set_const_str(enums.ENUM_DW_AT)
set_const_str(enums.ENUM_DW_FORM)
set_const_str(dwarf_expr.DW_OP_name2opcode)
set_const_raw(dwarf_const.__dict__, 'DW_ATE_')

DI_ROOT = -1

def format_di_key(di_key):
	get_suffix = lambda tag: '()' if tag == STR.DW_TAG_subprogram else ''
	return '::'.join(name + get_suffix(tag) for name, tag in di_key)

class ExprException(Exception):
	pass

class ExprVisitor(dwarf_expr.GenericExprVisitor):
	def __init__(self, structs):
		super(ExprVisitor, self).__init__(structs)
		self.__value = None

	def _after_visit(self, opcode, opcode_name, args):
		if opcode_name != STR.DW_OP_addr:
			raise ExprException("Unsupported opcode {0}".format(opcode_name))

		self.__value = args[0]

	def get_addr(self, expr):
		self.process_expr(expr)
		return self.__value

def get_die_name(die, empty_ok=False):
	attr = die.attributes.get(STR.DW_AT_name)
	if attr is None:
		if empty_ok:
			return
		raise Exception("DIE has no name")

	assert attr.form in [STR.DW_FORM_string, STR.DW_FORM_strp], attr.form
	return attr.value

def get_die_addr(die):
	structs = die.cu.structs

	if die.tag == STR.DW_TAG_subprogram:
		if STR.DW_AT_entry_pc in die.attributes:
			raise Exception("DW_AT_entry_pc is not supported")

		attr = die.attributes.get(STR.DW_AT_low_pc)
		if attr is None:
			return "no DW_AT_low_pc attribute"
		assert attr.form == STR.DW_FORM_addr, attr.form
		return attr.value

	elif die.tag == STR.DW_TAG_variable:
		const_value_attr = die.attributes.get(STR.DW_AT_const_value)
		if const_value_attr:
			return "is optimized out, has constant value {}".format(
				const_value_attr.value)

		if STR.DW_AT_declaration in die.attributes:
			return "is declaration"

		attr = die.attributes.get(STR.DW_AT_location)
		if attr is None:
			return "is optimized out"
		if attr.form != STR.DW_FORM_exprloc:
			return "location is not defined by DWARF expression"

		expr_visitor = ExprVisitor(structs)
		try:
			return expr_visitor.get_addr(attr.value)
		except ExprException:
			return "has no constant address"

	else:
		assert 0

def get_die_size(die):
	if die.tag in [STR.DW_TAG_compile_unit, STR.DW_TAG_subprogram]:
		low_pc  = die.attributes.get(STR.DW_AT_low_pc)
		high_pc = die.attributes.get(STR.DW_AT_high_pc)
		if low_pc is None or high_pc is None:
			raise Exception("DW_AT_{low,high}_pc attr missing. Non-continuos code?")

		high_pc_form = descriptions.describe_form_class(high_pc.form)
		if high_pc_form == "constant":
			return high_pc.value
		elif high_pc_form == "address":
			return high_pc.value - low_pc.value
		else:
			raise Exception("Unknown attribute form {}".format(high_pc.form))
	else:
		assert 0

def _iter_DIEs(cu, offset=None):
	cu_boundary = cu.cu_offset + cu['unit_length'] + cu.structs.initial_length_field_size()
	die_offset = cu.cu_die_offset if offset is None else offset
	# See CompileUnit._unflatten_tree()
	parent_stack = [DI_ROOT]

	while die_offset < cu_boundary:
		die = DIE(
			cu=cu,
			stream=cu.dwarfinfo.debug_info_sec.stream,
			offset=die_offset)

		if not die.is_null():
			yield die, parent_stack[-1]
			if die.has_children:
				parent_stack.append(die.offset)

		else:
			parent_stack.pop()

		if parent_stack[-1] == DI_ROOT:
			return

		die_offset += die.size


@memoize(WeakKeyDictionary)
def _read_CU(cu):
	pos_arr        = array.array('l', [-1])
	parent_pos_arr = array.array('l', [-1])

	for die, parent_pos in _iter_DIEs(cu):
		pos_arr.append(die.offset)
		parent_pos_arr.append(parent_pos)

	return pos_arr, parent_pos_arr

@memoize(WeakKeyDictionary)
def _get_line_program(cu):
	return cu.dwarfinfo.line_program_for_CU(cu)

@memoize(WeakKeyDictionary)
def get_CU_name(cu):
	cu_die, _ = _iter_DIEs(cu).next()
	if cu_die.tag != STR.DW_TAG_compile_unit:
		raise Exception("Unknown CU DIE tag {}".format(cu_die.tag))
	return get_die_name(cu_die)

class DebugInfoObject(object):
	def __init__(self, debug_info, die, parent_die_pos):
		self.debug_info		= debug_info
		self.die		= die
		self.tag		= die.tag
		self.attributes		= die.attributes
		self.die_pos		= die.offset
		self.parent_die_pos	= parent_die_pos
		self._str		= None

	def get_parent(self):
		pos = self.parent_die_pos
		return self.debug_info.get_dio_by_pos(pos) if pos >= 0 else None

	def iter_children(self, immediate=True):
		debug_info = self.debug_info
		pos = self.die.offset

		# islice() to skip self
		for die, parent_pos in itertools.islice(_iter_DIEs(self.die.cu, pos), 1, None):
			if not immediate or parent_pos == pos:
				yield DebugInfoObject(debug_info, die, parent_pos)

	def lookup(self, name):
		result = None
		for dio in self.iter_children():
			dio_name = dio.get_name(True)
			if not dio_name or dio_name != name:
				continue

			if result:
				raise Exception("Duplicate DIO name {}".format(name))
			result = dio

		return result


	def get_key(self):
		die = self.die

		if die.tag not in [STR.DW_TAG_subprogram, STR.DW_TAG_variable]:
			return

		skip_attrs = [
			STR.DW_AT_abstract_origin,
			STR.DW_AT_declaration,
			STR.DW_AT_artificial,
		]
		if set(die.attributes).intersection(skip_attrs):
			return

		key = []
		dio = self
		while dio:
			die = dio.die
			if die.tag == STR.DW_TAG_lexical_block:
				return
			sym_name = get_die_name(die)
			key.append((sym_name, die.tag))

			dio = dio.get_parent()

		key.reverse()
		return tuple(key)

	def get_name(self, empty_ok=False):
		return get_die_name(self.die, empty_ok)

	def get_src_location(self):
		file_num = self.attributes[STR.DW_AT_decl_file].value
		line_num = self.attributes[STR.DW_AT_decl_line].value

		lp = _get_line_program(self.die.cu)
		file_info = lp.header.file_entry[file_num - 1]
		file_name = file_info.name

		# DWARF standard v4 says:
		# ---
		# 12. file_names (sequence of file entries)
		# Entries in this sequence describe source files that contribute to the line number information
		# for this compilation unit or is used in other contexts, such as in a declaration coordinate or a
		# macro file inclusion. Each entry consists of the following values:
		# * A null-terminated string containing the full or relative path name of a source file. If the
		# entry contains a file name or relative path name, the file is located relative to either the
		# compilation directory (as specified by the DW_AT_comp_dir attribute given in the
		# compilation unit) or one of the directories listed in the include_directories section.
		# . . .
		# The directory index represents an entry in the include_directories section. The index is 0
		# if the file was found in the current directory of the compilation, 1 if it was found in the first
		# directory in the include_directories section, and so on. The directory index is ignored for
		# file names that represent full path names.
		# The primary source file is described by an entry whose path name exactly matches that given
		# in the DW_AT_name attribute in the compilation unit, and whose directory is understood to
		# be given by the implicit entry with index 0.
		# ---
		# So by this wording, it seems like for primary source file CU's DW_AT_name should be equal to
		# path name. However, in practice it equals path name with prepended directory name.

		dir_info = lp.header.include_directory[file_info.dir_index -1] if file_info.dir_index else None
		if dir_info is not None and not file_name.startswith('/'):
			file_name = dir_info + '/' + file_name

		return file_name, line_num

	def get_addr(self):
		addr = get_die_addr(self.die)
		if isinstance(addr, basestring):
			print("!! {} {}".format(self, addr))
			return

		assert isinstance(addr, (int, long))
		return addr

	def get_type(self):
		type_ref_forms = [STR.DW_FORM_ref1, STR.DW_FORM_ref2, STR.DW_FORM_ref4, STR.DW_FORM_ref8]
		type_attr = self.attributes[STR.DW_AT_type]
		if type_attr.form not in type_ref_forms:
			raise Exception("Unknown attr form {}".format(type_attr.form))
		type_pos = self.die.cu.cu_offset + type_attr.value
		return get_type_obj(self.debug_info.elf, type_pos)

	def get_value(self):
		assert self.tag == STR.DW_TAG_variable

		elf = self.debug_info.elf

		addr = self.get_addr()
		if addr is None:
			raise Exception("Can't get object address")

		stream = MemoryStream(elf)
		stream.seek(addr)
		return self.get_type().read(stream)

	def get_size(self):
		return get_die_size(self.die)

	def __str__(self):
		if self._str:
			return self._str

		loc = "<{:x}>".format(self.die.offset)
		key = self.get_key()
		self._str = "DIO<{} {}>".format(
			format_di_key(key) if key else self.die.tag, loc)
		return self._str
	
	__repr__ = __str__

class DebugInfo(object):
	def __init__(self, elf):
		self.elf = elf
		self._cu_pos  = cu_pos  = []
		self._cu_names = {}

		if not self.elf.has_dwarf_info():
			raise Exception("No debuginfo in ELF")
		dwi = self.elf.get_dwarf_info()

		for cu in dwi.iter_CUs():
			cu_pos.append((-cu.cu_offset, cu))
			cu_name = get_CU_name(cu)
			if cu_name in self._cu_names:
				self._cu_names[cu_name] = None
			self._cu_names[cu_name] = cu

		cu_pos.append((1, None))
		cu_pos.sort()

	def get_cu_names(self):
		return self._cu_names.keys()

	def get_dio_by_pos(self, pos):
		assert pos >= 0
		# Consider sorted array A  having no duplicate elements
		# [..., X, Y, ...], where X < Y, and some element P
		# If X < P < Y then bisect_left(P) == bisect_right(P) == index(Y)
		# as described at https://docs.python.org/2/library/bisect.html
		# IOW, bisection selects right end of the range. Finally, when
		# P is same as Y, these functions return different results:
		# bisect_left(P)  == index(Y)
		# bisect_right(P) == index(Y) + 1
		# So we use A[bisect_right(pos) - 1] to lookup DIEs.
		# When looking up CUs, situation is a bit different, since we store
		# 2-tuples in the array. To make comparisons possible, we should use 1-tuple as a key.
		# When position to look up matches CU offset, key tuple will be less than element tuple.
		# So subtracting one will give wrong result. To overcome this, we use negated offsets.
		# In such case, we want to select the right end, so to lookup CUs we use
		# A[bisect_right(key)]
		# bisect_right() is the same as bisect()
		cu_key = (-pos,)
		cu_idx = bisect.bisect(self._cu_pos, cu_key)
		_, cu = self._cu_pos[cu_idx]
		if not cu:
			return
		die_pos, die_parent_pos = _read_CU(cu)

		die_idx = bisect.bisect(die_pos, pos)
		assert die_idx > 0
		die_idx -= 1
		die_offset = die_pos[die_idx]
		if die_offset < 0:
			return

		# See CompileUnit._parse_DIEs()
		die = DIE(
			cu=cu,
			stream=cu.dwarfinfo.debug_info_sec.stream,
			offset=die_offset)
		within_die = die.offset <= pos < die.offset + die.size
		if not within_die:
			raise Exception("Position is outside DIE")
		return DebugInfoObject(self, die, die_parent_pos[die_idx])

	def get_dio_by_key(self, key):
		cu_name, die_type = key[0]
		assert die_type == STR.DW_TAG_compile_unit
		cu = self._cu_names[cu_name]

		dio = None
		for curr_dio in self._iter_cu_dios(cu):
			if curr_dio.get_key() != key:
				continue

			if dio:
				raise Exception("Duplicate key {0}".format(key))
			dio = curr_dio

		return dio

	def _iter_cu_dios(self, cu):
		for die, parent_pos in _iter_DIEs(cu):
			yield DebugInfoObject(self, die, parent_pos)
			
	def iter_dios(self):
		# Skip sentinel (1, None) at position zero
		for cu_pos, cu in itertools.islice(reversed(self._cu_pos), 1, None):
			for dio in self._iter_cu_dios(cu):
				yield dio

	def lookup(self, cu_name):
		cu = self._cu_names.get(cu_name)
		return DebugInfoObject(self, cu.get_top_DIE(), DI_ROOT) if cu else None

get_debug_info = memoize(WeakKeyDictionary)(DebugInfo)

class Struct(object):
	def __init__(self, **kwargs):
		for name, value in kwargs.iteritems():
			setattr(self, name, value)

	def __repr__(self):
		data = ["%s = %r" % (name, value)
				for name, value in self.__dict__.iteritems()
					if not name.startswith("__")]
		data.sort()
		return "Struct(%s)" % ", ".join(data)

	__str__ = __repr__

class TypeObject(object):
	def __init__(self, dio):
		self.dio = dio

		get_attr = lambda attr: dio.attributes[attr].value

		parent   = None
		is_base  = False
		size     = None
		signed   = None
		encoding = None
		members  = None
		name     = None
		offset   = None

		if dio.tag == STR.DW_TAG_pointer_type:
			size = get_attr(STR.DW_AT_byte_size)
			signed = False
			read_ptr = self._read_int

			parent = dio.get_type()
			if parent.is_base and parent.encoding in [
				RAW.DW_ATE_unsigned_char, RAW.DW_ATE_signed_char]:
				read = lambda stream: self._read_ptr_char(
							read_ptr(stream), stream)
			else:
				read = read_ptr 

		elif dio.tag in [STR.DW_TAG_typedef, STR.DW_TAG_const_type]:
			parent = dio.get_type()
			read = parent.read

		elif dio.tag == STR.DW_TAG_structure_type:
			size = get_attr(STR.DW_AT_byte_size)

			members = []
			for child_dio in dio.iter_children():
				assert child_dio.tag == STR.DW_TAG_member
				members.append(TypeObject(child_dio))

			read = self._read_struct

		elif dio.tag == STR.DW_TAG_member:
			name = get_attr(STR.DW_AT_name)
			offset = get_attr(STR.DW_AT_data_member_location)
			parent = dio.get_type()
			read = parent.read

		elif dio.tag == STR.DW_TAG_base_type:
			is_base = True
			name = get_attr(STR.DW_AT_name)
			size = get_attr(STR.DW_AT_byte_size)

			encoding = get_attr(STR.DW_AT_encoding)
			if encoding in [RAW.DW_ATE_unsigned, RAW.DW_ATE_unsigned_char]:
				signed = False
			elif encoding in [RAW.DW_ATE_signed, RAW.DW_ATE_signed_char]:
				signed = True
			else:
				assert 0

			read = self._read_int

		else:
			assert 0

		self.parent   = parent
		self.is_base  = is_base
		self.size     = size
		self.signed   = signed
		self.encoding = encoding
		self.members  = members
		self.name     = name
		self.offset   = offset

		self.read = read

	def __str__(self):
		die = self.dio.die
		loc = "<{:x}>".format(die.offset)
		return "TYPE<{} {}>".format(die.tag, loc)

	__repr__ = __str__

	def _read_int(self, stream):
		data = stream.read(self.size)
		return rtoi(data, self.signed)

	def _read_ptr_char(self, ptr, stream):
		if not ptr:
			return None

		saved_pos = stream.tell()
		stream.seek(ptr)

		null = "\x00"
		chunks = []
		scan = True
		while scan:
			data = stream.read(1<<10, allow_short=True)
			null_idx = data.find(null)
			if null_idx >= 0:
				data = data[:null_idx]
				scan = False
			chunks.append(data)

		stream.seek(saved_pos)
		return "".join(chunks)

	def _read_struct(self, stream):
		data = {}
		pos = stream.tell()

		for member in self.members:
			stream.seek(pos + member.offset)
			data[member.name] = member.read(stream)

		stream.seek(pos + self.size)
		return Struct(**data)

@memoize(WeakKeyDictionary, dict)
def get_type_obj(elf, pos):
	debug_info = get_debug_info(elf)
	dio = debug_info.get_dio_by_pos(pos)
	return TypeObject(dio)

