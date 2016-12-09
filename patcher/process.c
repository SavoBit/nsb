#include <stdio.h>
#include <sys/mman.h>
#include <sys/user.h>

#include <compel/compel.h>
#include <compel/ptrace.h>

#include "include/log.h"
#include "include/xmalloc.h"
#include "include/vma.h"

#include "include/process.h"

extern int compel_syscall(struct parasite_ctl *ctl,
			  int nr, unsigned long *ret,
			  unsigned long arg1,
			  unsigned long arg2,
			  unsigned long arg3,
			  unsigned long arg4,
			  unsigned long arg5,
			  unsigned long arg6);

struct patch_place_s {
	struct list_head	list;
	unsigned long		start;
	unsigned long		size;
	unsigned long		used;
};

int process_write_data(pid_t pid, void *addr, void *data, size_t size)
{
	return ptrace_poke_area(pid, data, addr, size);
}

int process_read_data(pid_t pid, void *addr, void *data, size_t size)
{
	return ptrace_peek_area(pid, data, addr, size);
}

int64_t process_create_map(struct process_ctx_s *ctx, int fd, off_t offset,
			unsigned long addr, size_t size, int flags, int prot)
{
	int ret;
	long sret = -ENOSYS;

	ret = compel_syscall(ctx->ctl, __NR(mmap, false), (unsigned long *)&sret,
			     addr, size, prot, flags, fd, offset);
	if (ret < 0) {
		pr_err("Failed to execute syscall for %d\n", ctx->pid);
		return -1;
	}

	if (sret < 0) {
		errno = -sret;
		pr_perror("Failed to create mmap with size %zu bytes", size);
		return -1;
	}

	pr_debug("Created map %#lx-%#lx in task %d\n",
		 sret, sret + size, ctx->pid);

	return sret;
}

static struct patch_place_s *find_place(struct binpatch_s *bp, unsigned long hint)
{
	struct patch_place_s *place;

	list_for_each_entry(place, &bp->places, list) {
		if ((place->start & 0xffffffff00000000) == (hint & 0xffffffff00000000)) {
			pr_debug("found place for patch: %#lx (hint: %#lx)\n",
					place->start, hint);
			return place;
		}
	}
	return NULL;
}

static struct patch_place_s *alloc_place(unsigned long addr, size_t size)
{
	struct patch_place_s *place;

	place = xmalloc(sizeof(*place));
	if (!place) {
		pr_err("failed to allocate\n");
		return NULL;
	}
	place->start = addr;
	place->size = size;
	place->used = 0;

	return place;
}

static unsigned long process_find_hole(struct process_ctx_s *ctx, unsigned long hint, size_t size)
{
	unsigned long addr;

	addr = find_vma_hole(&ctx->vmas, hint, size);
	if (addr)
		return addr;
	return -ENOENT;
}

static int process_create_place(struct process_ctx_s *ctx, unsigned long hint,
				size_t size, struct patch_place_s **place)
{
	long ret;
	unsigned long addr;
	struct binpatch_s *bp = &ctx->binpatch;
	struct patch_place_s *p;

	size = round_up(size, PAGE_SIZE);

	addr = process_find_hole(ctx, hint, size);
	if (addr < 0) {
		pr_err("failed to find address hole by hint %#lx\n", hint);
		return -EFAULT;
	}

	pr_debug("Found hole: %#lx-%#lx\n", addr, addr + size);

	p = alloc_place(addr, size);
	if (!p)
		return -ENOMEM;

	/* TODO: need drop PROT_WRITE at the end */
	ret = process_create_map(ctx, -1, 0,
				 p->start, p->size,
				 MAP_ANONYMOUS | MAP_PRIVATE,
				 PROT_READ | PROT_WRITE | PROT_EXEC);
	if ((void *)ret == MAP_FAILED) {
		pr_err("failed to create remove mem\n");
		goto destroy_place;
	}

	if (ret != p->start) {
		pr_err("mmap result doesn't match expected: %ld != %ld\n",
				ret, p->start);
		goto unmap_remote;
	}

	list_add_tail(&p->list, &bp->places);

	pr_debug("created new place for patch: %#lx-%#lx (hint: %#lx)\n",
			p->start, p->start + p->size, hint);

	*place = p;
	return 0;

unmap_remote:
	/* TODO here remote map has to be unmapped */
destroy_place:
	free(p);
	return ret;
}

long process_get_place(struct process_ctx_s *ctx, unsigned long hint, size_t size)
{
	struct binpatch_s *bp = &ctx->binpatch;
	struct patch_place_s *place;
	long addr;

	/* Aling function size by 16 bytes */
	size = round_up(size, 16);

	place = find_place(bp, hint);
	if (!place) {
		int ret;

		ret = process_create_place(ctx, hint, size, &place);
		if (ret)
			return ret;
	} else if (place->size - place->used < size) {
		pr_err("No place left for %ld bytes in vma %#lx (free: %ld)\n",
				size, place->start, place->size - place->used);
		return -ENOMEM;
	}

	addr = place->start + round_up(place->used, 16);
	place->used += size;
	return addr;
}

int process_cure(struct process_ctx_s *ctx)
{
	pr_debug("Resume from %d\n", ctx->pid);
	if (compel_resume_task(ctx->pid, TASK_ALIVE, TASK_ALIVE)) {
		pr_err("Can't unseize from %d\n", ctx->pid);
		return -1;
	}
	return 0;
}

int process_infect(struct process_ctx_s *ctx)
{
	int ret;

	ret = compel_stop_task(ctx->pid);
	pr_debug("Stopping... %s\n", (ret != TASK_ALIVE) ? "FAIL" : "OK");
	if (ret != TASK_ALIVE)
		return ret;

	ctx->ctl = compel_prepare(ctx->pid);
	if (!ctx->ctl) {
		pr_err("Can't create compel control\n");
		return -1;
	}

	if (collect_vmas(ctx->pid, &ctx->vmas)) {
		pr_err("Can't collect mappings for %d\n", ctx->pid);
		goto err;
	}
	print_vmas(ctx->pid, &ctx->vmas);

	return 0;

err:
	process_cure(ctx);
	return -1;
}
