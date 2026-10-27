import {useEffect, useRef, useState} from 'react';

import {useT} from '@/src/lib/i18n';
import {panelRouteTitleKey} from '@/src/lib/routes';

export function RouteAnnouncer({pathname}: {pathname: string}) {
  const t = useT();
  const targetRef = useRef<HTMLDivElement>(null);
  const [announcement, setAnnouncement] = useState('');
  const title = t(panelRouteTitleKey(pathname));

  useEffect(() => {
    document.title = `${title} · Tobkiri Launcher`;
    setAnnouncement(`${title} opened`);
    targetRef.current?.focus({preventScroll: true});
  }, [pathname, title]);

  return (
    <div
      ref={targetRef}
      role="status"
      aria-live="polite"
      aria-atomic="true"
      tabIndex={-1}
      className="sr-only"
    >
      {announcement}
    </div>
  );
}
