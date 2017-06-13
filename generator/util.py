import functools

def memoize(*dict_classes):
	first_class  = dict_classes[0]
	rest_classes = dict_classes[1:]

	def fix_dict_class(f):
		cache = first_class()

		@functools.wraps(f)
		def wrapper(*args):
			assert len(args) == len(dict_classes)

			res = cache
			for x in args:
				res = res.get(x)
				if res is None:
					break
			else:
				return res

			c = cache
			for x, dc in zip(args, rest_classes):
				cn = c.get(x)
				if cn is None:
					cn = c[x] = dc()
				c = cn

			res = c[args[-1]] = f(*args)
			return res

		return wrapper

	return fix_dict_class

