"""Run the actual added charger-policy C functions against failing I/O boundaries."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'patches/linux/7.3/0106-power-piano-charge-limit.patch'


def patched_function(name):
    source = '\n'.join(line[1:] for line in PATCH.read_text().splitlines()
                       if line.startswith(('+', ' ')) and not line.startswith('+++'))
    prefix = ('static bool ' if name == 'piano_mca_charge_inhibited' else
              'static void ' if name == 'piano_mca_update_end_threshold' else 'static int ')
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
typedef uint32_t u32;
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define max(a,b) ((a)>(b)?(a):(b))
#define MCA_PROP_CHARGE_ENABLE 0x2003
#define MCA_OP_READ 1
#define MCA_CP_OFF 0
#define MCA_CP_DONE 1
#define MCA_CP_ON 2
#define MCA_CP_OPENING 3
#define MCA_FFC_IBAT_MAX_MA 12000
#define MCA_FCC_MIN_MA 100
#define POWER_SUPPLY_PROP_CAPACITY 0
#define POWER_SUPPLY_PROP_CHARGE_CONTROL_END_THRESHOLD 1
#define POWER_SUPPLY_PROP_CHARGE_CONTROL_LIMIT 2
#define POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR 3
#define POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO 0
#define POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE 1
enum power_supply_property { UNUSED_PROPERTY = 9 };
union power_supply_propval { int intval; };
struct power_supply { void *data; };
struct piano_mca {
    void *client;
    unsigned charge_end_threshold, charge_limit_ua;
    bool end_latched, end_paused, inhibit_charge, inhibit_stopped_cp;
    bool normal_full, policy_stopped, policy_suspended, service_up;
    int cp, poll, policy_lock;
};
static bool charge_policy=true, cp_charge=true, charge_current=true;
static const char *piano_mca_supplied_to[] = {"gauge0", "gauge1"};
static struct power_supply gauges[2];
static int soc[2], gauge_error, missing_gauge, cp_error, pause_error;
static int cp_stops, pauses, requests, notifications;
static int charge_read_error, charge_write_error, charge_actual, charge_writes;
static bool lose_ack, ignore_write;
static int system_freezable_wq;
static unsigned pmic_glink_get_generation(void *client) { (void)client; return 7; }
static unsigned piano_mca_connection_generation(struct piano_mca *m) { (void)m; return 8; }
static struct power_supply *power_supply_get_by_name(const char *name) {
    int i = !strcmp(name,"gauge1");
    return missing_gauge == i+1 ? NULL : &gauges[i];
}
static int power_supply_get_property(struct power_supply *psy, int p,
                                      union power_supply_propval *v) {
    assert(p == POWER_SUPPLY_PROP_CAPACITY);
    if (gauge_error) return gauge_error;
    v->intval = soc[psy == &gauges[1]]; return 0;
}
static void power_supply_put(struct power_supply *psy) { assert(psy); }
static bool piano_mca_cp_active(struct piano_mca *m) { return m->cp == MCA_CP_ON || m->cp==MCA_CP_OPENING; }
static int piano_mca_cp_stop(struct piano_mca *m, const char *why) {
    (void)why; cp_stops++;
    if (cp_error) return cp_error;
    m->cp = MCA_CP_DONE; return 0;
}
static int piano_mca_cp_pause(struct piano_mca *m, const char *why) { return piano_mca_cp_stop(m,why); }
static int piano_mca_normal_pause(struct piano_mca *m, unsigned g, unsigned c) {
    (void)m; assert(g==7 && c==8); pauses++; return pause_error;
}
static u32 piano_mca_charge_limit(struct piano_mca *m) { return m->charge_limit_ua/1000; }
static int piano_mca_update_charge_limit(struct piano_mca *m, u32 old) { (void)m;(void)old;return 0; }
static void power_supply_changed(struct power_supply *p) { (void)p; notifications++; }
static void *power_supply_get_drvdata(struct power_supply *p) { return p->data; }
static void mutex_lock(int *p) { assert(!*p); *p = 1; }
static void mutex_unlock(int *p) { assert(*p); *p = 0; }
static void mod_delayed_work(int q, int *p, unsigned long delay) {
    (void)q; (void)p; assert(!delay); requests++;
}
static int piano_mca_xfer(struct piano_mca *m, int op, int prop, void *data,
                          size_t n, unsigned g, unsigned c) {
    (void)m; assert(op==MCA_OP_READ && prop==MCA_PROP_CHARGE_ENABLE && n==1 && g==7 && c==8);
    if (charge_read_error) return charge_read_error;
    *(u8 *)data=charge_actual; return 0;
}
static int piano_mca_write_session(struct piano_mca *m, int prop, void *data,
                                   size_t n, unsigned g, unsigned c) {
    (void)m; assert(prop==MCA_PROP_CHARGE_ENABLE && n==1 && g==7 && c==8);
    charge_writes++;
    if (!ignore_write && (!charge_write_error || lose_ack)) charge_actual=*(u8 *)data;
    return charge_write_error;
}
'''

CASES = r'''
int main(void) {
    struct piano_mca m = {.charge_end_threshold=100,.cp=MCA_CP_ON,.service_up=true};
    struct power_supply psy = {.data=&m};
    union power_supply_propval v = {.intval=80};
    soc[0]=soc[1]=70;
    piano_mca_update_end_threshold(&m); assert(!m.end_paused && !m.end_latched);
    assert(!piano_mca_usb_set_property(&psy,1,&v) && m.charge_end_threshold==80 && requests==1);
    int before=notifications;
    assert(!piano_mca_usb_set_property(&psy,1,&v) && notifications==before);
    v.intval=49; assert(piano_mca_usb_set_property(&psy,1,&v)==-EINVAL);
    v.intval=101; assert(piano_mca_usb_set_property(&psy,1,&v)==-EINVAL);
    v.intval=80; assert(piano_mca_usb_set_property(&psy,UNUSED_PROPERTY,&v)==-EINVAL);
    assert(piano_mca_usb_property_is_writeable(&psy,1));
    assert(!piano_mca_usb_property_is_writeable(&psy,UNUSED_PROPERTY));
    soc[1]=80; piano_mca_update_end_threshold(&m);
    assert(m.end_latched && m.end_paused && piano_mca_charge_inhibited(&m));
    assert(!piano_mca_pause_charging(&m,7,8) && m.inhibit_stopped_cp);
    assert(cp_stops==1 && pauses==1);
    assert(piano_mca_normal_enable(&m,true,7,8)==-EPERM && charge_writes==0);
    /* A manual AUTO request cannot clear the capacity cap or reopen the pump. */
    v.intval=POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO;
    assert(!piano_mca_usb_set_property(&psy,POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR,&v));
    assert(m.end_paused && m.cp==MCA_CP_DONE);
    soc[1]=79; piano_mca_update_end_threshold(&m);assert(m.end_paused);
    soc[0]=75; soc[1]=76;piano_mca_update_end_threshold(&m);assert(m.end_paused);
    soc[1]=75;piano_mca_update_end_threshold(&m);assert(!m.end_paused && !m.end_latched);
    assert(charge_writes==0);
    m.inhibit_charge=true;assert(piano_mca_charge_inhibited(&m));
    assert(piano_mca_normal_enable(&m,true,7,8)==-EPERM);
    m.inhibit_charge=false;
    soc[0]=80;piano_mca_update_end_threshold(&m);assert(m.end_paused);
    v.intval=100;assert(!piano_mca_usb_set_property(&psy,1,&v));
    assert(!m.end_paused && !m.end_latched && charge_writes==0);

    m.charge_end_threshold=80;soc[0]=soc[1]=70;
    gauge_error=-EIO;piano_mca_update_end_threshold(&m);assert(m.end_paused);
    gauge_error=0;missing_gauge=2;piano_mca_update_end_threshold(&m);assert(m.end_paused);
    missing_gauge=0;soc[0]=101;piano_mca_update_end_threshold(&m);assert(m.end_paused);
    soc[0]=-1;piano_mca_update_end_threshold(&m);assert(m.end_paused);
    soc[0]=70;piano_mca_update_end_threshold(&m);assert(!m.end_paused);
    m.policy_suspended=true;v.intval=100;
    assert(piano_mca_usb_set_property(&psy,1,&v)==-EBUSY && m.charge_end_threshold==80);
    m.policy_suspended=false;m.policy_stopped=true;
    assert(piano_mca_usb_set_property(&psy,1,&v)==-ENODEV && m.charge_end_threshold==80);
    m.policy_stopped=false;
    charge_policy=false;assert(!piano_mca_usb_property_is_writeable(&psy,1));
    assert(piano_mca_usb_set_property(&psy,1,&v)==-EOPNOTSUPP);charge_policy=true;

    soc[0]=80;m.cp=MCA_CP_ON;cp_error=-EBUSY;v.intval=80;
    assert(piano_mca_usb_set_property(&psy,1,&v)==-EBUSY && m.end_paused && m.inhibit_stopped_cp);
    cp_error=0;pause_error=-ETIMEDOUT;
    assert(piano_mca_usb_set_property(&psy,1,&v)==-ETIMEDOUT && m.end_paused);
    pause_error=0;assert(!piano_mca_usb_set_property(&psy,1,&v));
    m.service_up=false;assert(piano_mca_pause_charging(&m,7,8)==-ENODEV);m.service_up=true;
    before=pauses;assert(!piano_mca_pause_charging(&m,7,8) && pauses==before+1);

    m.end_paused=false;charge_actual=1;
    assert(!piano_mca_normal_enable(&m,true,7,8) && charge_writes==1);
    m.normal_full=true;assert(piano_mca_normal_enable(&m,true,7,8)==-EPERM);m.normal_full=false;
    assert(!piano_mca_normal_enable(&m,false,7,8) && charge_actual==0 && charge_writes==2);
    charge_read_error=-EIO;
    assert(piano_mca_normal_enable(&m,true,7,8)==-EIO && charge_writes==2);
    assert(piano_mca_normal_enable(&m,false,7,8)==-EIO && charge_writes==3);
    charge_read_error=0;charge_actual=1;ignore_write=true;
    assert(piano_mca_normal_enable(&m,false,7,8)==-EIO);
    ignore_write=false;charge_write_error=-ETIMEDOUT;lose_ack=true;
    assert(piano_mca_normal_enable(&m,false,7,8)==-ETIMEDOUT && charge_actual==0);
    charge_write_error=0;assert(!piano_mca_normal_enable(&m,false,7,8));
    assert(!piano_mca_normal_enable(&m,true,7,8) && charge_actual==1);
    return 0;
}
'''


class ChargeLimitTests(unittest.TestCase):
    def test_real_policy_with_cell_imbalance_io_errors_and_charge_feedback(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('C compiler unavailable')
        functions = '\n\n'.join(patched_function(name) for name in (
            'piano_mca_charge_inhibited', 'piano_mca_charge_soc',
            'piano_mca_update_end_threshold', 'piano_mca_pause_charging',
            'piano_mca_normal_enable', 'piano_mca_usb_set_property',
            'piano_mca_usb_property_is_writeable'))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'limit.c'
            executable = Path(directory) / 'limit'
            source.write_text(PRELUDE + functions + CASES)
            subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                            '-Wno-unused-parameter', '-fsanitize=address,undefined',
                            '-fno-omit-frame-pointer', str(source), '-o', str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    unittest.main()
