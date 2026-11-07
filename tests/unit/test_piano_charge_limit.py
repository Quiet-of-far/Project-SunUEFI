"""Run the actual added charger-policy C functions against failing I/O boundaries."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'patches/linux/7.3/0029-power-piano-charge-limit.patch'


def patched_function(name):
    source = '\n'.join(line[1:] for line in PATCH.read_text().splitlines()
                       if line.startswith(('+', ' ')) and not line.startswith('+++'))
    prefix = 'static void ' if name in ('piano_mca_complete', 'piano_mca_queue_poll', 'piano_mca_pause_poll') else 'static int '
    start = source.index(prefix + name + '(')
    begin = source.index('{', start)
    depth = 1
    end = begin + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <errno.h>
#include <string.h>
typedef uint8_t u8;
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define max(a,b) ((a)>(b)?(a):(b))
#define MCA_PROP_CHARGE_ENABLE 0x2003
#define MCA_CP_OFF 0
#define MCA_CP_DONE 1
#define MCA_CP_ON 2
#define POWER_SUPPLY_PROP_ONLINE 2
#define POWER_SUPPLY_PROP_CAPACITY 0
#define POWER_SUPPLY_PROP_CHARGE_CONTROL_END_THRESHOLD 1
enum power_supply_property { UNUSED_PROPERTY = 9 };
union power_supply_propval { int intval; };
struct device { void *data; };
struct power_supply { void *data; };
struct piano_mca {
    struct device *dev;
    struct power_supply *usb;
    unsigned charge_limit, charge_generation, pdr_generation;
    bool limit_latched, charge_state_valid, charging_enabled;
    bool charge_managed, limit_cp_stopped;
    bool online, service_up, sleeping, stopping;
    int cp, poll, policy_lock, poll_lock;
};
static const char *piano_mca_supplied_to[] = {"gauge0", "gauge1"};
static struct power_supply gauges[2];
static int soc[2], gauge_error, missing_gauge, cp_error, write_error;
static int writes, last_enabled, cp_stops, sequence, stop_order, write_order;
static int requests, notifications, cancels;
static bool restart_in_write;
static int stage_read_error, stage_on_reads, sleeps;
static int piano_mca_cp_get(struct piano_mca *m, int prop, int *on) {
    (void)m; assert(prop == POWER_SUPPLY_PROP_ONLINE);
    if (stage_read_error) return stage_read_error;
    *on = stage_on_reads > 0;
    if (stage_on_reads) stage_on_reads--;
    return 0;
}
static void msleep(int delay) { assert(delay == 100); sleeps++; }

static int system_percpu_wq;
static struct power_supply *power_supply_get_by_name(const char *name) {
    int i = !strcmp(name,"gauge1");
    if (missing_gauge == i+1) return NULL;
    return &gauges[i];
}
static int power_supply_get_property(struct power_supply *psy, int p,
                                      union power_supply_propval *v) {
    assert(p == POWER_SUPPLY_PROP_CAPACITY);
    if (gauge_error) return gauge_error;
    v->intval = soc[psy == &gauges[1]]; return 0;
}
static void power_supply_put(struct power_supply *psy) { assert(psy); }
static bool piano_mca_cp_active(struct piano_mca *m) { return m->cp == MCA_CP_ON; }
static int piano_mca_cp_stop(struct piano_mca *m, const char *why) {
    (void)why;
    if (!piano_mca_cp_active(m)) return 0;
    cp_stops++; stop_order = ++sequence;
    if (cp_error) return cp_error;
    m->cp = MCA_CP_DONE; return 0;
}
static int piano_mca_write(struct piano_mca *m, unsigned p, const void *data, size_t n) {
    assert(p == MCA_PROP_CHARGE_ENABLE && n == 1 && *(const u8 *)data <= 1);
    writes++; last_enabled = *(const u8 *)data; write_order = ++sequence;
    if (restart_in_write) { m->pdr_generation++; restart_in_write = false; }
    return write_error;
}
#define dev_info(...) ((void)0)
static void power_supply_changed(struct power_supply *p) { (void)p; notifications++; }
static void *power_supply_get_drvdata(struct power_supply *p) { return p->data; }
static void *dev_get_drvdata(struct device *d) { return d->data; }
static void mutex_lock(int *p) { assert(!*p); *p = 1; }
static void mutex_unlock(int *p) { assert(*p); *p = 0; }
static void mod_delayed_work(int q, int *p, unsigned long delay) { (void)q;(void)p;assert(!delay);requests++; }
#define spin_lock_irqsave(p,f) do { (f)=0; mutex_lock(p); } while(0)
#define spin_unlock_irqrestore(p,f) do { (void)(f); mutex_unlock(p); } while(0)
#define spin_lock_irq(p) mutex_lock(p)
#define spin_unlock_irq(p) mutex_unlock(p)
static void cancel_delayed_work_sync(int *p) { (void)p; cancels++; }
'''

CASES = r'''
static struct piano_mca fresh(struct power_supply *psy, struct device *dev) {
    struct piano_mca m = {.charge_limit=100,.online=true,.service_up=true,
                           .pdr_generation=1,.usb=psy,.dev=dev,.cp=MCA_CP_ON};
    gauge_error=missing_gauge=cp_error=write_error=0;
    writes=cp_stops=sequence=stop_order=write_order=requests=notifications=cancels=0;
    restart_in_write=false; soc[0]=soc[1]=70;
    return m;
}
int main(void) {
    struct power_supply psy = {0}; struct device dev = {0};
    struct piano_mca m = fresh(&psy,&dev); psy.data=dev.data=&m;
    union power_supply_propval v = {.intval=80};
    assert(!piano_mca_cp_wait_stopped(&m) && sleeps==0);
    stage_on_reads=3;
    assert(!piano_mca_cp_wait_stopped(&m) && sleeps==3);
    stage_on_reads=100; sleeps=0;
    assert(piano_mca_cp_wait_stopped(&m)==-EBUSY && sleeps==5);
    stage_read_error=-EIO;
    assert(piano_mca_cp_wait_stopped(&m)==-EIO);
    stage_read_error=stage_on_reads=0;

    assert(!piano_mca_apply_charge_limit(&m,false) && writes==0 && cp_stops==0);
    assert(!piano_mca_usb_set_property(&psy,1,&v) && m.charge_limit==80 && requests==1);
    v.intval=49; assert(piano_mca_usb_set_property(&psy,1,&v)==-EINVAL);
    v.intval=101; assert(piano_mca_usb_set_property(&psy,1,&v)==-EINVAL);
    v.intval=80; assert(piano_mca_usb_set_property(&psy,UNUSED_PROPERTY,&v)==-EINVAL);
    assert(piano_mca_usb_property_is_writeable(&psy,1));
    assert(!piano_mca_usb_property_is_writeable(&psy,UNUSED_PROPERTY));
    assert(!piano_mca_apply_charge_limit(&m,false) && last_enabled==1 && writes==1);
    soc[1]=80;
    assert(!piano_mca_apply_charge_limit(&m,false) && m.limit_latched && !m.charging_enabled);
    assert(cp_stops==1 && stop_order<write_order && last_enabled==0);
    int before=writes;
    soc[0]=soc[1]=79; assert(!piano_mca_apply_charge_limit(&m,false) && writes==before);
    soc[0]=soc[1]=76; assert(!piano_mca_apply_charge_limit(&m,false) && writes==before);
    soc[0]=soc[1]=75;
    assert(!piano_mca_apply_charge_limit(&m,false) && m.charging_enabled && m.cp==MCA_CP_OFF);
    assert(!m.limit_latched && writes==before+1);
    soc[0]=80; assert(!piano_mca_apply_charge_limit(&m,false));
    m.pdr_generation++; before=writes;
    assert(!piano_mca_apply_charge_limit(&m,false) && writes==before+1 && last_enabled==0);
    m.online=false;
    assert(!piano_mca_apply_charge_limit(&m,false) && m.charging_enabled && !m.limit_latched);
    m.online=true; assert(!piano_mca_apply_charge_limit(&m,false) && !m.charging_enabled);
    v.intval=100; assert(!piano_mca_usb_set_property(&psy,1,&v));
    assert(!piano_mca_apply_charge_limit(&m,false) && m.charging_enabled);
    m.service_up=false; assert(!piano_mca_apply_charge_limit(&m,false));

    m=fresh(&psy,&dev); m.charge_limit=80; soc[0]=101;
    assert(!piano_mca_apply_charge_limit(&m,false) && !m.charging_enabled);
    soc[0]=70; gauge_error=-EIO;
    assert(!piano_mca_apply_charge_limit(&m,false) && !m.charging_enabled);
    gauge_error=0; missing_gauge=2;
    assert(!piano_mca_apply_charge_limit(&m,false) && !m.charging_enabled);
    missing_gauge=0;
    assert(!piano_mca_apply_charge_limit(&m,false) && m.charging_enabled);
    assert(!piano_mca_apply_charge_limit(&m,true) && !m.charging_enabled);
    m.online=false;
    assert(!piano_mca_apply_charge_limit(&m,true) && !m.charging_enabled);

    m=fresh(&psy,&dev); m.charge_limit=80; soc[0]=80; cp_error=-EIO;
    assert(piano_mca_apply_charge_limit(&m,false)==-EIO && last_enabled==0 && m.cp==MCA_CP_ON);
    cp_error=0; assert(!piano_mca_apply_charge_limit(&m,false) && m.cp==MCA_CP_DONE);
    m=fresh(&psy,&dev); m.charge_limit=80;
    assert(!piano_mca_apply_charge_limit(&m,false) && m.charging_enabled);
    soc[0]=80; write_error=-ETIMEDOUT;
    assert(piano_mca_apply_charge_limit(&m,false)==-ETIMEDOUT && !m.charge_state_valid);
    write_error=0; m.charge_limit=100;
    assert(!piano_mca_apply_charge_limit(&m,false) && m.charging_enabled);
    m=fresh(&psy,&dev); m.charge_limit=80; soc[0]=80; write_error=-ETIMEDOUT;
    assert(piano_mca_apply_charge_limit(&m,false)==-ETIMEDOUT && !m.charge_state_valid);
    assert(m.charge_managed && m.limit_cp_stopped);
    write_error=0; m.charge_limit=100;
    assert(!piano_mca_apply_charge_limit(&m,false) && m.charging_enabled);
    assert(m.cp==MCA_CP_OFF && !m.limit_cp_stopped);
    m.charge_limit=80;
    assert(!piano_mca_apply_charge_limit(&m,false) && !m.charging_enabled);
    m.pdr_generation++; restart_in_write=true;
    assert(piano_mca_apply_charge_limit(&m,false)==-EAGAIN);
    assert(m.charge_generation!=m.pdr_generation);
    assert(!piano_mca_apply_charge_limit(&m,false) && m.charge_generation==m.pdr_generation);
    m.service_up=false; before=writes;
    assert(piano_mca_apply_charge_limit(&m,false)==-ENODEV && writes==before);

    m=fresh(&psy,&dev); m.charge_limit=80;
    assert(!piano_mca_prepare(&dev) && m.sleeping && last_enabled==0 && cancels==1);
    piano_mca_complete(&dev);
    assert(!m.sleeping && requests==1);
    assert(!piano_mca_apply_charge_limit(&m,false) && m.charging_enabled);
    m.online=false;
    assert(!piano_mca_prepare(&dev) && !m.charging_enabled);
    piano_mca_complete(&dev);
    m.service_up=false; before=requests;
    assert(piano_mca_prepare(&dev)==-ENODEV && !m.sleeping && requests==before+1);
    m.stopping=true; before=requests;
    assert(piano_mca_usb_set_property(&psy,1,&v)==-ENODEV && requests==before);
    piano_mca_complete(&dev); assert(requests==before);
    return 0;
}
'''


@unittest.skipUnless(shutil.which('cc'), 'C compiler is required')
class PianoChargeLimitTests(unittest.TestCase):
    def test_actual_policy_with_failed_transport_and_sleep(self):
        names = ('piano_mca_queue_poll', 'piano_mca_pause_poll', 'piano_mca_cp_wait_stopped', 'piano_mca_charge_soc', 'piano_mca_apply_charge_limit',
                 'piano_mca_usb_set_property', 'piano_mca_usb_property_is_writeable',
                 'piano_mca_prepare', 'piano_mca_complete')
        source = PRELUDE + '\n'.join(patched_function(name) for name in names) + CASES
        with tempfile.TemporaryDirectory(prefix='charge-policy-') as temporary:
            folder = Path(temporary)
            path = folder / 'policy.c'
            path.write_text(source)
            result = subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                                     '-Wno-unused-parameter', '-fsanitize=address,undefined',
                                     '-fno-pie', '-no-pie', str(path), '-o', str(folder / 'policy')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(folder / 'policy')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
