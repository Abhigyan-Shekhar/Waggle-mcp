import React, { useState, useEffect, useRef, useMemo } from "react";

/**
 * VirtualList - A high-performance virtualization list for React 19.
 * Renders only the items in the viewport using a top-and-bottom spacer approach.
 * Supports dynamic item heights by measuring rendered elements and caching their heights.
 */
export function VirtualList({
  items,
  renderItem,
  estimatedItemHeight = 120,
  buffer = 5,
  className = "",
  itemKey,
  footer,
  spacingClass = "pb-3"
}) {
  const containerRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [containerHeight, setContainerHeight] = useState(600);
  const [measuredHeights, setMeasuredHeights] = useState({});
  const itemRefs = useRef({});

  // Get unique key for an item
  const getItemKey = (item, index) => {
    if (itemKey) return itemKey(item, index);
    return item.id || item.key || index;
  };

  // Monitor container scroll and height
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleScroll = () => {
      setScrollTop(container.scrollTop);
    };

    // Use ResizeObserver to dynamically update container height
    const resizeObserver = new ResizeObserver((entries) => {
      for (let entry of entries) {
        setContainerHeight(entry.contentRect.height || 600);
      }
    });

    container.addEventListener("scroll", handleScroll, { passive: true });
    resizeObserver.observe(container);

    // Initial values
    setScrollTop(container.scrollTop);
    setContainerHeight(container.getBoundingClientRect().height || 600);

    return () => {
      container.removeEventListener("scroll", handleScroll);
      resizeObserver.disconnect();
    };
  }, []);

  // Measure rendered items and update heights in state if changed
  useEffect(() => {
    let changed = false;
    const newHeights = { ...measuredHeights };

    Object.entries(itemRefs.current).forEach(([indexStr, el]) => {
      if (el) {
        const index = parseInt(indexStr, 10);
        const item = items[index];
        if (item) {
          const key = getItemKey(item, index);
          const height = el.getBoundingClientRect().height;
          // Use a small epsilon to avoid unnecessary updates from floating point precision
          if (newHeights[key] === undefined || Math.abs(newHeights[key] - height) > 0.5) {
            newHeights[key] = height;
            changed = true;
          }
        }
      }
    });

    if (changed) {
      setMeasuredHeights(newHeights);
    }
  }); // Run on every render to measure the DOM elements

  // Compute prefix sums of heights (offsets) and total height
  const { offsets, totalHeight } = useMemo(() => {
    const offsets = [];
    let currentOffset = 0;
    for (let i = 0; i < items.length; i++) {
      offsets.push(currentOffset);
      const key = getItemKey(items[i], i);
      const height = measuredHeights[key] !== undefined ? measuredHeights[key] : estimatedItemHeight;
      currentOffset += height;
    }
    offsets.push(currentOffset); // Add total height at the end
    return { offsets, totalHeight: currentOffset };
  }, [items, measuredHeights, estimatedItemHeight, itemKey]);

  // Find start and end indices using binary search
  const { startIndex, endIndex } = useMemo(() => {
    if (items.length === 0) {
      return { startIndex: 0, endIndex: 0 };
    }

    // Binary search for the first visible item (first item ending after scrollTop)
    let lowStart = 0;
    let highStart = items.length - 1;
    let start = items.length - 1;
    while (lowStart <= highStart) {
      const mid = (lowStart + highStart) >> 1;
      if (offsets[mid + 1] > scrollTop) {
        start = mid;
        highStart = mid - 1;
      } else {
        lowStart = mid + 1;
      }
    }
    const startIndex = Math.max(0, start - buffer);

    // Binary search for the first item starting after the viewport
    let lowEnd = startIndex;
    let highEnd = items.length - 1;
    let end = items.length;
    const viewportEnd = scrollTop + containerHeight;
    while (lowEnd <= highEnd) {
      const mid = (lowEnd + highEnd) >> 1;
      if (offsets[mid] >= viewportEnd) {
        end = mid;
        highEnd = mid - 1;
      } else {
        lowEnd = mid + 1;
      }
    }
    const endIndex = Math.min(items.length, end + buffer);

    return { startIndex, endIndex };
  }, [offsets, scrollTop, containerHeight, buffer, items.length]);

  const topSpacerHeight = offsets[startIndex] || 0;
  const bottomSpacerHeight = Math.max(0, totalHeight - (offsets[endIndex] || totalHeight));

  const visibleItems = useMemo(() => {
    const rendered = [];
    for (let i = startIndex; i < endIndex; i++) {
      const item = items[i];
      if (!item) continue;
      
      const key = getItemKey(item, i);
      rendered.push(
        <div
          key={key}
          ref={(el) => {
            if (el) {
              itemRefs.current[i] = el;
            } else {
              delete itemRefs.current[i];
            }
          }}
          className={spacingClass}
        >
          {renderItem(item, i)}
        </div>
      );
    }
    return rendered;
  }, [startIndex, endIndex, items, renderItem, itemKey, spacingClass]);

  return (
    <div
      ref={containerRef}
      className={className}
      style={{
        position: "relative",
        overflowY: "auto",
        WebkitOverflowScrolling: "touch",
      }}
    >
      <div style={{ height: topSpacerHeight, flexShrink: 0 }} />
      {visibleItems}
      <div style={{ height: bottomSpacerHeight, flexShrink: 0 }} />
      {footer}
    </div>
  );
}
