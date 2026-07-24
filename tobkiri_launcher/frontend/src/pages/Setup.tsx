import { useState, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { useAppStore } from '@/src/store';
import { Button } from '@/src/components/ui/Button';
import { useT } from '@/src/lib/i18n';
import { panelRoutes } from '@/src/lib/routes';
import {
  SETUP_PACK_RETURN_PARAM,
  hasSelectedSetupPack,
  setupPackSelectionUrl,
} from '@/src/lib/setupPacks';
import { ArrowRight, CheckCircle2 } from 'lucide-react';
import { TobkiriLoadingMark } from '@/src/components/ui/TobkiriLoader';
import { motion } from 'motion/react';
import { LAUNCHER_DISPLAY_NAME } from '@/src/lib/launcherBrand';
import tobkiriIconUrl from '../../../assets/app-icon/tobkiri-launcher-icon.png';

export function Setup() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const setSetupDone = useAppStore(state => state.setSetupDone);
  const connectAccount = useAppStore(state => state.connectAccount);
  const loadProfile = useAppStore(state => state.loadProfile);
  const profile = useAppStore(state => state.profile);
  const addToast = useAppStore(state => state.addToast);
  const t = useT();
  const [loading, setLoading] = useState(false);
  const [linked, setLinked] = useState(false);
  const [setupPackError, setSetupPackError] = useState<string | null>(null);

  const finalizeSetup = async (): Promise<boolean> => {
    if (!await hasSelectedSetupPack()) {
      return false;
    }
    setSetupDone(true);
    return true;
  };

  const openSetupPackSelection = () => {
    const colorMode = document.documentElement.dataset.colorMode;
    window.location.assign(setupPackSelectionUrl(undefined, colorMode));
  };

  useEffect(() => {
    void loadProfile();
  }, [loadProfile]);

  useEffect(() => {
    const refreshProfile = () => {
      void loadProfile();
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') {
        refreshProfile();
      }
    };

    window.addEventListener('focus', refreshProfile);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    return () => {
      window.removeEventListener('focus', refreshProfile);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
    };
  }, [loadProfile]);

  // Handle OAuth callback redirect params
  useEffect(() => {
    const isLinked = searchParams.get('linked');
    const error = searchParams.get('error');

    if (isLinked === 'true') {
      let alive = true;
      let timer: ReturnType<typeof setTimeout> | null = null;
      setLoading(true);
      void finalizeSetup()
        .then((completed) => {
          if (!alive) return;
          if (!completed) {
            openSetupPackSelection();
            return;
          }
          setLinked(true);
          addToast(t('setup.link_success') || 'Account linked successfully!', 'success');
          timer = setTimeout(() => {
            navigate(panelRoutes.home);
          }, 1500);
        })
        .catch((setupError) => {
          if (!alive) return;
          setSetupPackError(setupError instanceof Error ? setupError.message : 'Setup pack selection failed');
          setLoading(false);
        });
      return () => {
        alive = false;
        if (timer) clearTimeout(timer);
      };
    }

    if (error) {
      addToast(`OAuth error: ${error}`, 'error');
    }
  }, [searchParams, addToast, navigate, t]);

  useEffect(() => {
    if (searchParams.get(SETUP_PACK_RETURN_PARAM) !== '1') {
      return;
    }

    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setLoading(true);
    void finalizeSetup()
      .then((completed) => {
        if (!alive) return;
        if (!completed) {
          setSetupPackError('Choose and install a setup pack before opening the panel.');
          setLoading(false);
          return;
        }
        setLinked(true);
        addToast(t('setup.link_success') || 'Account linked successfully!', 'success');
        timer = setTimeout(() => {
          navigate(panelRoutes.home);
        }, 800);
      })
      .catch((setupError) => {
        if (!alive) return;
        setSetupPackError(setupError instanceof Error ? setupError.message : 'Setup pack selection failed');
        setLoading(false);
      });

    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [searchParams, addToast, navigate, t]);

  useEffect(() => {
    if (!profile.connected || linked || searchParams.get(SETUP_PACK_RETURN_PARAM) === '1') {
      return;
    }

    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setLoading(true);
    void finalizeSetup()
      .then((completed) => {
        if (!alive) return;
        if (!completed) {
          openSetupPackSelection();
          return;
        }
        setLinked(true);
        addToast(t('setup.link_success') || 'Account linked successfully!', 'success');
        timer = setTimeout(() => {
          navigate(panelRoutes.home);
        }, 1500);
      })
      .catch(() => {
        if (!alive) return;
        addToast(t('setup.connect_failed') || 'Failed to connect', 'error');
        setLoading(false);
      });

    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [profile.connected, linked, searchParams, addToast, navigate, t]);

  const handleConnect = async () => {
    setLoading(true);
    try {
      await connectAccount();
      addToast(
        t('setup.connect_started') || 'Browser opened. Finish signing in there, then return.',
        'success',
      );
    } catch {
      addToast(t('setup.connect_failed') || 'Failed to connect', 'error');
    } finally {
      setLoading(false);
    }
  };

  const handleSkip = () => {
    setSetupPackError(null);
    openSetupPackSelection();
  };

  if (linked) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg-main p-6">
        <motion.div initial={{opacity: 0, scale: .96}} animate={{opacity: 1, scale: 1}} className="relative flex w-full max-w-sm flex-col items-center gap-6 text-center">
          <motion.div initial={{scale: .8}} animate={{scale: 1}} transition={{type: 'spring', stiffness: 260, damping: 22}} className="flex h-14 w-14 items-center justify-center rounded-xl border border-border bg-bg-card">
            <CheckCircle2 className="h-8 w-8 text-emerald-500" />
          </motion.div>
          <div>
            <h1 className="text-xl font-semibold text-text-main">{t('setup.linked_title') || 'Account Linked!'}</h1>
            <p className="mt-2 text-sm text-text-muted">{t('setup.redirecting') || 'Redirecting to dashboard...'}</p>
          </div>
          <TobkiriLoadingMark scene="startup" />
        </motion.div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen bg-bg-main">
      <div className="mx-auto grid w-full max-w-4xl items-center gap-10 px-6 py-10 lg:grid-cols-[1fr_400px]">
        <motion.section initial={{opacity: 0, x: -18}} animate={{opacity: 1, x: 0}} transition={{duration: .45}} className="max-w-xl">
          <div className="mb-10 flex items-center gap-3 text-sm font-semibold text-text-main">
            <img
              src={tobkiriIconUrl}
              alt="Tobkiri"
              className="h-9 w-9 rounded-lg border border-border bg-bg-card object-cover"
            />
            {LAUNCHER_DISPLAY_NAME}
          </div>
          <div>
            <span className="text-xs font-medium text-text-muted">初期設定</span>
            <h1 className="mt-3 text-3xl font-semibold tracking-[-.035em] text-text-main sm:text-4xl">Tobkiriをセットアップ</h1>
            <p className="mt-4 max-w-md text-sm leading-7 text-text-muted">アカウントと起動時に読み込むpackを設定します。どちらもあとから変更できます。</p>
          </div>
          <div className="mt-9 space-y-3 border-l border-border pl-4 text-xs leading-5 text-text-muted">
            <p>1. アカウントを接続</p>
            <p>2. 起動時のpackと権限を確認</p>
          </div>
        </motion.section>

        <motion.section initial={{opacity: 0, y: 14}} animate={{opacity: 1, y: 0}} transition={{delay: .06, duration: .36}} className="rounded-[18px] border border-border bg-bg-card p-6 shadow-lg sm:p-7">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-text-muted">初期セットアップ</span>
            <span className="rounded-full border border-border bg-bg-main px-2.5 py-1 text-[10px] font-semibold text-text-muted">1 / 2</span>
          </div>
          <div className="mt-4 flex gap-1.5"><i className="h-1 flex-1 rounded-full bg-text-main" /><i className="h-1 flex-1 rounded-full bg-border" /></div>
          <h2 className="mt-7 text-lg font-semibold text-text-main">アカウント</h2>
          <p className="mt-2 text-sm leading-6 text-text-muted">接続するとプロファイルを同期できます。接続せずにpack選択へ進むこともできます。</p>
          <div className="mt-7 flex flex-col gap-3">
            <Button size="lg" className="w-full justify-between" onClick={handleConnect} disabled={loading} loading={loading}>
              <span>{t('setup.connect_rumi')}</span>{!loading && <ArrowRight className="h-4 w-4" />}
            </Button>
            <Button variant="outline" size="lg" className="w-full" onClick={handleSkip} disabled={loading}>{t('setup.choose_packs')}</Button>
          </div>
          {setupPackError && <p className="mt-4 rounded-xl border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-500">{setupPackError}</p>}
          <p className="mt-6 border-t border-border pt-4 text-[11px] leading-5 text-text-muted">packごとの権限は次の画面で確認します。</p>
        </motion.section>
      </div>
    </div>
  );
}
