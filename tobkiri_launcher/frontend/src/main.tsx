import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App.tsx';
import { ErrorBoundary } from './components/ui/ErrorBoundary.tsx';
import { bootstrapDocumentAppearance } from './lib/appearance.ts';
import './index.css';

performance.mark('tobkiri:app-entry');
bootstrapDocumentAppearance();

function DevelopmentCrashProbe(): never {
  throw new Error('Development-only ErrorBoundary QA probe');
}

const content = import.meta.env.DEV
  && new URLSearchParams(window.location.search).get('qa-error-boundary') === '1'
  ? <DevelopmentCrashProbe />
  : <App />;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      {content}
    </ErrorBoundary>
  </StrictMode>,
);
