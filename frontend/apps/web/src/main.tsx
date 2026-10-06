import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';
import { router } from './router';
import { ProjectScopeProvider } from './scope';
import { ErrorBoundary } from './components/ui';
import '@fontsource/instrument-serif/400.css';
import '@fontsource/courier-prime/400.css';
import '@fontsource/courier-prime/700.css';
import './styles/tokens.css';
import './styles/base.css';
import { applyTheme } from './shell/theme';

applyTheme();

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } });

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* Whole-app last resort: if the shell itself throws, still show calm copy
        on a centred screen rather than a blank white page. */}
    <ErrorBoundary page={false}>
      <QueryClientProvider client={queryClient}>
        <ProjectScopeProvider>
          <RouterProvider router={router} />
        </ProjectScopeProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
