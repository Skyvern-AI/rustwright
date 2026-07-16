<?php

declare(strict_types=1);

namespace Rustwright;

final class Browser
{
    private bool $closed = false;
    private bool $freed = false;

    /** @var list<\WeakReference<Page>> */
    private array $pages = [];

    public function __construct(
        private readonly NativeLibrary $native,
        private mixed $handle,
    ) {
    }

    public function newPage(): Page
    {
        $this->ensureOpen();
        $page = new Page($this->native, $this->native->browserNewPage($this->handle));
        $this->pages[] = \WeakReference::create($page);
        return $page;
    }

    public function wsEndpoint(): string
    {
        $this->ensureOpen();
        return $this->native->browserWsEndpoint($this->handle);
    }

    public function close(): void
    {
        if ($this->freed) {
            return;
        }

        $firstError = null;
        foreach ($this->pages as $reference) {
            $page = $reference->get();
            if ($page === null) {
                continue;
            }
            try {
                $page->close();
            } catch (\Throwable $error) {
                $firstError ??= $error;
            }
        }
        $this->pages = [];

        try {
            if (!$this->closed) {
                $this->native->browserClose($this->handle);
                $this->closed = true;
            }
        } catch (\Throwable $error) {
            $firstError ??= $error;
        } finally {
            $this->native->browserFree($this->handle);
            $this->handle = null;
            $this->freed = true;
        }

        if ($firstError !== null) {
            throw $firstError;
        }
    }

    public function __destruct()
    {
        try {
            $this->close();
        } catch (\Throwable) {
            // Destructors cannot safely surface lifecycle errors.
        }
    }

    private function ensureOpen(): void
    {
        if ($this->closed || $this->freed) {
            throw new RustwrightException('Browser is closed');
        }
    }
}
