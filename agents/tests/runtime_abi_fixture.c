/* ABI fixtures from ROCr hsa.h/hsa_ext_amd.h and Vulkan-Headers public contracts.
 * Deliberately use non-zero upper pointer/handle bits and distinct PCI addresses. */
#include <stdint.h>
#include <string.h>

typedef struct { uint64_t handle; } agent;
typedef uint32_t (*callback)(agent, void *);
#define HANDLE ((void *)(uintptr_t)0x123456789abcdef0ULL)
uint32_t hsa_init(void) { return 0; }
uint32_t hsa_shut_down(void) { return 0; }
uint32_t hsa_iterate_agents(callback cb, void *data) { return cb((agent){(uintptr_t)HANDLE}, data); }
uint32_t hsa_agent_get_info(agent a, uint32_t what, void *out) {
    if (a.handle != (uintptr_t)HANDLE) return 1;
    if (what == 17) { *(uint32_t *)out = 1; return 0; } /* HSA_AGENT_INFO_DEVICE, GPU */
    if (what == 0xA006) { *(uint32_t *)out = 0x1a03; return 0; } /* bus 1a, device 0, function 3 */
    if (what == 0xA00F) { *(uint32_t *)out = 2; return 0; }
    if (what == 0xA011) { memcpy(out, "GPU-0123456789abcdef", 21); return 0; }
    return 1;
}
typedef struct { uint32_t s; void *next; } base;
typedef struct { uint32_t s; void *next; unsigned char uuid[16]; } idp;
typedef struct { uint32_t s; void *next; uint32_t domain, bus, device, function; } pcip;
int32_t vkCreateInstance(void *a, void *b, void **out) { (void)a; (void)b; *out = HANDLE; return 0; }
void vkDestroyInstance(void *a, void *b) { (void)a; (void)b; }
int32_t vkEnumeratePhysicalDevices(void *i, uint32_t *n, void **p) {
    if (i != HANDLE) return -1;
    if (!p) { *n = 1; return 0; }
    p[0] = HANDLE; return 0;
}
int32_t vkEnumerateDeviceExtensionProperties(void *p, char *x, uint32_t *n, void *out) {
    (void)x;
    if (p != HANDLE) return -1;
    if (!out) { *n = 1; return 0; }
    memcpy(out, "VK_EXT_pci_bus_info", 20); return 0;
}
void vkGetPhysicalDeviceProperties2(void *p, base *props) {
    if (p != HANDLE || props->s != 1000059001) return;
    idp *id = props->next;
    if (id->s != 1000071004) return;
    pcip *pci = id->next;
    if (pci->s != 1000212000) return;
    for (int i = 0; i < 16; i++) id->uuid[i] = (unsigned char)i;
    pci->domain = 2; pci->bus = 0x1a; pci->device = 3; pci->function = 0;
}
